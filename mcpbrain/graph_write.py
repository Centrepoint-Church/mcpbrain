"""Structural write path for the enrichment graph (Phase 1, Task 2).

Ports the Nexus `memory_db` / `bitemporal_writer` write functions, repointed at
a `mcpbrain.store.Store` instance. The single public entry point is `apply()`,
which consumes one thread's extraction JSON and writes entities, relations,
topics, role observations, and the email_context row.

No DDL lives here: every write goes through Store methods or `store._connect()`.
The table DDL is owned by store.py.

Casing seam: the Nexus `_DOMAIN_ORG` map and KNOWN_ORGS use lowercase canonical
org tags ("orgname", "acc"). The mcpbrain contract and `enrich._VALID_ORGS`
use DISPLAY forms ("OrgName", "ACC"). This module adopts display-case as
canonical so org tags match the extraction contract and email_context rows.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from nameparser import HumanName

from mcpbrain import config, orgs
from mcpbrain.resolve import build_entity_index, write_time_dedup_check, add_to_index
from mcpbrain.chunking import (  # dependency-free; no graph_write -> enrich coupling
    slugify,
    _normalise_title_for_dedup,
    action_fingerprint as _compute_fingerprint,
)

log = logging.getLogger("mcpbrain.graph_write")


# ---------------------------------------------------------------------------
# Install-owner identity (config-driven)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OwnerIdentity:
    """Who this install belongs to, for action attribution and self-exclusion.

    name:      short name written to actions.owner
    entity_id: the owner's entity slug — never upserted as an entity; used to
               recognise and skip the owner in the graph
    aliases:   lowercased name variants treated as the owner
    """
    name: str = ""
    entity_id: str = ""
    aliases: frozenset = field(default_factory=frozenset)


def owner_identity_from_config() -> OwnerIdentity:
    """Build the owner identity from config.json under MCPBRAIN_HOME."""
    home = str(config.app_dir())
    return OwnerIdentity(
        name=config.owner_name(home),
        entity_id=slugify(config.owner_full_name(home)),
        aliases=config.owner_aliases(home),
    )

# ---------------------------------------------------------------------------
# Org / entity utilities (ported from memory_db.py:1428-1519)
#
# The taxonomy itself now lives in orgs.py (config-driven, DEFAULT_TAXONOMY =
# the historical four orgs). The module-level names below are kept as
# default-taxonomy views for back-compat importers; runtime paths resolve the
# configured taxonomy via orgs.taxonomy_from_config() — apply() resolves once
# per call and threads it down.
# ---------------------------------------------------------------------------

_DOMAIN_ORG = dict(orgs.DEFAULT_TAXONOMY.domain_map)

KNOWN_ORGS: frozenset = frozenset(_DOMAIN_ORG.values())

# Human-readable "domain -> Org" lines for the enrich prompt's org_domain_map
# (default-taxonomy view; prepare reads the configured taxonomy directly).
_DOMAIN_ORG_LINES = list(orgs.DEFAULT_TAXONOMY.domain_lines)

# Free-text org strings → display-case canonical org (default-taxonomy view).
ORG_ALIASES = dict(orgs.DEFAULT_TAXONOMY.aliases)

_JUNK_PATTERNS = [
    re.compile(r"^(Re|Fwd|FW|RE|FWD)\s*:", re.IGNORECASE),
    re.compile(r"https?://"),
    re.compile(r"\w+@\w+\.\w+"),
    re.compile(r"[|{}\[\]<>]"),
]

# Numeric patterns applied to person names ONLY: a 4-digit run (year/amount) or
# a date fragment is almost always junk in a person name, but legitimate in an
# org/project name ("OrgName 2026"). This split mirrors enrich._is_junk_entity
# rather than the Nexus is_junk_entity, which rejected 4-digit runs for orgs too.
_NUMERIC_JUNK = [
    re.compile(r"\d{4}"),
    re.compile(r"\d{2,}/\d{2,}"),
]


def canonical_org(raw: str, taxonomy: "orgs.OrgTaxonomy | None" = None) -> str:
    """Resolve a free-text org string to its display-case canonical form.

    taxonomy=None resolves the configured taxonomy from config (which falls
    back to the historical default on an unconfigured install).
    """
    if taxonomy is None:
        taxonomy = orgs.taxonomy_from_config()
    return taxonomy.canonical(raw)


def org_from_email(email_addr: str, taxonomy: "orgs.OrgTaxonomy | None" = None) -> str:
    """Map an email address to its display-case org via the domain table.

    "" for empty input, "external" for an unrecognised domain.
    """
    if taxonomy is None:
        taxonomy = orgs.taxonomy_from_config()
    return taxonomy.from_email(email_addr)


def strip_title(name: str) -> tuple[str, str]:
    """Return (cleaned_name, original). Strips an honorific via HumanName."""
    original = name.strip()
    parsed = HumanName(original)
    if parsed.title:
        parts = [parsed.first, parsed.middle, parsed.last, parsed.suffix]
        cleaned = " ".join(p for p in parts if p)
        if not cleaned:
            return original, original
        return cleaned, original
    return original, original


# Org-classification TAGS — the org enum values (default-taxonomy view).
# A relation endpoint that is an EXACT (case-insensitive) match to one of these
# is a classification tag, not a real entity, so the relation is rejected. Real
# org names that merely contain a tag word ("Acme Corp") are not tags.
# apply() uses the configured taxonomy's org_tags; this stays for importers.
_ORG_TAGS = orgs.DEFAULT_TAXONOMY.org_tags


# ` from <X>` / ` at <X>` affiliation suffix on a PERSON name. The head group
# is required non-empty (so "from The Church Co" is left alone). Surrounding
# spaces and the trailing-word anchor keep "Atherton" / "Bank of Melbourne"
# safe — only the standalone words "from"/"at" trigger.
_AFFILIATION_SUFFIX_RE = re.compile(
    r"^(?P<head>.+?)\s+(?:from|at)\s+\S.*$", re.IGNORECASE)


def strip_affiliation(name: str) -> str:
    """Drop a trailing ` from <X>` / ` at <X>` affiliation from a PERSON name.

    "Franz from The Church Co" -> "Franz"; "Tim at TechCorp" -> "Tim". Returns
    the name unchanged when there is no such suffix, when the head would be
    empty, or for ` of `/` with ` (not stripped — org-shaped names use those).
    Only call this on PERSON names; org/project names legitimately carry
    "of"/"at"/"from".
    """
    if not name:
        return name
    stripped = name.strip()
    match = _AFFILIATION_SUFFIX_RE.match(stripped)
    if not match:
        return name
    head = match.group("head").strip()
    return head if head else name


def is_junk_entity(name: str, entity_type: str) -> bool:
    """Reject obviously-bad person/org entities.

    Structural patterns (URL, email, subject prefix, brackets) apply to person
    and org. Numeric patterns (4-digit runs, date fragments) apply to person
    only — org/project names may legitimately carry a year.
    """
    if entity_type not in ("person", "org"):
        return False
    name = name.strip()
    if len(name) < 2 or len(name) > 60:
        return True
    for pattern in _JUNK_PATTERNS:
        if pattern.search(name):
            return True
    if entity_type == "person":
        for pattern in _NUMERIC_JUNK:
            if pattern.search(name):
                return True
    return False


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Role observations (ported from memory_db.py:895-936, 1100-1143)
# ---------------------------------------------------------------------------

_SOURCE_RANK = {
    "manual": 5,
    "profile_audit": 4,   # audited corrections rank alongside email_signature
    "email_signature": 4,
    "from_header": 3,
    "domain_match": 3,
    "body_mention": 2,
    "enrichment": 2,
    "notion": 2,
    "gdrive": 2,
    "pipeline_snapshot": 1,
    "llm_extraction": 1,
    "session": 1,
}

# Contextual/social roles that are not formal job titles. Rejected at write time.
_JUNK_ROLE_VALUES = {
    "null", "unknown", "", "organiser", "organizer",
    "bbq & serving food", "bbq & serving food team",
    "judge", "scorer", "judges scorer",
    "creator", "pastor's health",
    "volunteer", "helper", "attendee", "participant",
    "worker", "field worker", "cleaners", "cleaner",
    "editor", "auditor", "presenter", "facilitator", "member",
    "guest speaker", "speaker", "panellist", "panelist",
}
# Alias kept for parity with the Nexus name used elsewhere in that module.
_JUNK_ROLES = _JUNK_ROLE_VALUES

# Sources below this rank are blocked from writing a role when an authoritative
# source already covers the entity. Derived from the dict so a rename fails loudly.
_BODY_MENTION_RANK: int = _SOURCE_RANK["body_mention"]


def _source_rank(source) -> int:
    """Source rank, stripping any colon-namespace prefix ("session:abc" -> "session")."""
    if not source:
        return 0
    key = source.split(":", 1)[0]
    return _SOURCE_RANK.get(key, 0)


def fetch_role(store, entity_id: str, *, current_only: bool = True) -> str:
    """Return the most recent non-invalidated 'role' observation, or ''.

    current_only=True (default): also requires valid_to IS NULL, returning only
    observations that are still in effect (used by profile_audit). False: returns
    the most recent observation regardless of valid_to (used by profile_synth).
    Single canonical implementation replacing duplicate _fetch_role helpers in
    profile_synth.py and profile_audit.py (§9C).
    """
    valid_to_clause = "AND (valid_to IS NULL OR valid_to = '')" if current_only else ""
    with store._connect() as db:
        row = db.execute(
            f"""SELECT value FROM entity_observations
               WHERE  entity_id = ?
                 AND  attribute  = 'role'
                 AND  (invalidated_at IS NULL OR invalidated_at = '')
                 {valid_to_clause}
               ORDER  BY valid_from DESC, id DESC
               LIMIT  1""",
            (entity_id,),
        ).fetchone()
    return row["value"] if row else ""


def write_role_observation(store, entity_id: str, title: str, source: str,
                           valid_from: str, confidence: str) -> None:
    """Write a 'role' observation with provenance + supersession.

    Ported from memory_db.py:1100-1143, repointed at store._connect(). Rejects
    over-long values and junk roles; skips low-ranked sources when an
    authoritative role already exists; supersedes a prior same-source role by
    setting its valid_to.
    """
    if len(title) > 80:
        return
    if title.strip().lower() in _JUNK_ROLE_VALUES:
        return

    with store._connect() as conn:
        # Skip low-ranked sources when an authoritative observation already exists.
        if _source_rank(source) < _BODY_MENTION_RANK:
            if conn.execute(
                "SELECT 1 FROM entity_observations "
                "WHERE entity_id = ? AND attribute = 'role' AND valid_to IS NULL "
                "AND source IN ('email_signature', 'from_header', 'body_mention')",
                (entity_id,),
            ).fetchone():
                return

        existing = conn.execute(
            "SELECT value FROM entity_observations "
            "WHERE entity_id = ? AND attribute = 'role' AND source = ? AND valid_to IS NULL",
            (entity_id, source),
        ).fetchone()
        if existing and existing[0] == title:
            return
        # Recency rule: retire prior same-source roles only if they are older-or-equal
        # (so an older role arriving late doesn't unseat a newer one). If a NEWER
        # same-source role is already current, the incoming older one is inserted as
        # historical (valid_to = that newer role's valid_from).
        conn.execute(
            "UPDATE entity_observations SET valid_to = ? "
            "WHERE entity_id = ? AND attribute = 'role' AND source = ? "
            "AND valid_to IS NULL AND valid_from <= ?",
            (valid_from, entity_id, source, valid_from),
        )
        newer = conn.execute(
            "SELECT MIN(valid_from) FROM entity_observations "
            "WHERE entity_id = ? AND attribute = 'role' AND source = ? "
            "AND valid_to IS NULL AND valid_from > ?",
            (entity_id, source, valid_from),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO entity_observations "
            "(entity_id, attribute, value, source, valid_from, valid_to, confidence_source) "
            "VALUES (?, 'role', ?, ?, ?, ?, ?)",
            (entity_id, title, source, valid_from, newer, confidence),
        )


# ---------------------------------------------------------------------------
# Bitemporal relation upsert (ported from bitemporal_writer.py + relation_kinds.py)
# ---------------------------------------------------------------------------

SINGLETON_RELATIONS = frozenset({"reports_to", "works_at"})
ACCUMULATING_RELATIONS = frozenset({
    "manages", "coordinates_with", "mentioned_with", "attended", "involved_in",
    "authored", "instance_of", "collaborates_with",
})

CONFIDENCE_BUMP = 0.05


def is_singleton_relation(relation: str) -> bool:
    return relation in SINGLETON_RELATIONS


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_conflicting_relations(conn, entity_a, relation):
    """All currently-valid rows for (entity_a, relation), regardless of entity_b."""
    return conn.execute(
        "SELECT id, entity_b, valid_from, valid_to, confidence, invalidated_at "
        "FROM entity_relations "
        "WHERE entity_a = ? AND relation = ? AND invalidated_at IS NULL",
        (entity_a, relation),
    ).fetchall()


def _mark_superseded(conn, relation_id, *, valid_to, invalidated_at, reason,
                     invalidated_by_relation_id):
    conn.execute(
        "UPDATE entity_relations "
        "SET valid_to = ?, invalidated_at = ?, superseded_reason = ?, "
        "    invalidated_by_relation_id = ? "
        "WHERE id = ?",
        (valid_to, invalidated_at, reason, invalidated_by_relation_id, relation_id),
    )


def _bump_observation(conn, relation_id, *, last_seen, confidence_delta):
    conn.execute(
        "UPDATE entity_relations "
        "SET last_seen = ?, "
        "    confidence = MIN(1.0, MAX(0.0, COALESCE(confidence, 1.0) + ?)) "
        "WHERE id = ?",
        (last_seen, confidence_delta, relation_id),
    )


def _increment_degree(conn, entity_a, entity_b):
    """Bump degree on both endpoints. Replaces the Nexus degree triggers, which
    this port does not carry. Only called on a NEW row insert, never on a
    re-observation or supersession."""
    conn.execute(
        "UPDATE entities SET degree = degree + 1 WHERE id IN (?, ?)",
        (entity_a, entity_b),
    )


def upsert_relation(store, entity_a, relation, entity_b, *, valid_from,
                    evidence="", confidence=1.0, strength=1,
                    source_doc_id: str | None = None) -> int:
    """Insert a bi-temporal relation with automatic supersession.

    Ported from bitemporal_writer.upsert_relation_bitemporal, repointed at
    store._connect(). Returns the surviving row id (the new row, or the existing
    row on a re-observation). The Nexus `created_at` column is dropped — the
    mcpbrain entity_relations table has no such column.

    The legacy UNIQUE(entity_a,relation,entity_b) does NOT block re-observation:
    a same-target observation bumps the existing row rather than inserting, and
    a same-target observation of a SUPERSEDED row revives that row (the UNIQUE
    spans invalidated rows, so inserting a fresh one is impossible — the
    2026-06-05 drain failures were exactly this). degree is incremented on both
    endpoints only for a NEW row; supersession and revival never touch it.

    source_doc_id: the originating document id for provenance. Defaults to
    evidence for backward compatibility when not explicitly supplied.
    """
    # Self-loops (an entity related to itself) are always noise — drop at the one
    # chokepoint every relation path flows through, regardless of relation type.
    if entity_a == entity_b:
        return None
    if source_doc_id is None:
        source_doc_id = evidence
    if not valid_from:
        raise ValueError("valid_from is required for bi-temporal writes")
    confidence = max(0.0, min(1.0, float(confidence)))
    now = _now_iso()
    with store._connect() as conn:
        conflicts = _find_conflicting_relations(conn, entity_a, relation)
        same_target = [c for c in conflicts if c["entity_b"] == entity_b]

        if same_target:
            rid = same_target[0]["id"]
            _bump_observation(conn, rid, last_seen=now, confidence_delta=CONFIDENCE_BUMP)
            return rid

        # Recency rule for SINGLETON relations (works_at/reports_to): a person has
        # one current value, and the CURRENT one is the newest-dated. Under backfill
        # (arbitrary order) an older fact can arrive after a newer one, so compare
        # valid_from rather than assuming the incoming write is the latest.
        singleton = is_singleton_relation(relation)
        others = [c for c in conflicts if c["entity_b"] != entity_b]
        newest_other = max(others, key=lambda c: ((c["valid_from"] or ""), c["id"]),
                           default=None) if singleton else None
        incoming_is_current = (newest_other is None
                               or (valid_from or "") >= (newest_other["valid_from"] or ""))

        # A superseded row for this exact pair blocks INSERT via the legacy UNIQUE.
        # Revive it: the fact is observed again. It becomes current from valid_from
        # UNLESS a newer rival is already current (then it is revived as historical).
        invalidated = conn.execute(
            "SELECT id FROM entity_relations "
            "WHERE entity_a = ? AND relation = ? AND entity_b = ? "
            "AND invalidated_at IS NOT NULL",
            (entity_a, relation, entity_b),
        ).fetchone()
        if invalidated is not None:
            new_id = invalidated["id"]
            conn.execute(
                "UPDATE entity_relations "
                "SET valid_from = ?, valid_to = NULL, invalidated_at = NULL, "
                "    superseded_reason = NULL, invalidated_by_relation_id = NULL, "
                "    confidence = ?, evidence = ?, strength = ?, last_seen = ?, "
                "    source_doc_id = ? "
                "WHERE id = ?",
                (valid_from, confidence, evidence, strength, now, source_doc_id, new_id),
            )
        else:
            new_id = conn.execute(
                "INSERT INTO entity_relations "
                "(entity_a, relation, entity_b, valid_from, confidence, evidence, strength, last_seen, source_doc_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_a, relation, entity_b, valid_from, confidence, evidence, strength, now, source_doc_id),
            ).lastrowid
            _increment_degree(conn, entity_a, entity_b)

        if singleton and incoming_is_current:
            # Newest fact → it is current; retire the older rivals.
            for c in others:
                _mark_superseded(
                    conn, c["id"], valid_to=valid_from, invalidated_at=now,
                    reason="superseded_by_newer", invalidated_by_relation_id=new_id,
                )
        elif singleton and not incoming_is_current:
            # An OLDER fact arrived late → record it but keep the newer rival current.
            _mark_superseded(
                conn, new_id, valid_to=newest_other["valid_from"], invalidated_at=now,
                reason="older_than_current", invalidated_by_relation_id=newest_other["id"],
            )
        return new_id


# ---------------------------------------------------------------------------
# Message / sender parsing (ported from enrich_gmail.py:131-142, 396-413)
# ---------------------------------------------------------------------------

SYSTEM_LABELS = {
    "INBOX", "UNREAD", "SENT", "STARRED", "IMPORTANT", "TRASH", "SPAM",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}

# Relation types the structural pass accepts. Mirrors enrich_gmail.py:1542.
VALID_RELATION_TYPES = {
    "works_at", "reports_to", "manages", "coordinates_with", "mentioned_with",
}

# Endpoint-type constraints for the person-centric relations. The LLM over-applies
# these to topics/projects/meetings ("a topic works_at an org"), which is nonsense
# and dilutes the graph — so a relation is dropped when an endpoint's resolved
# entity type isn't allowed. Relations not listed here (mentioned_with, …) accept
# any endpoint type. (source allowed types, target allowed types):
_RELATION_ENDPOINT_TYPES = {
    "works_at":   ({"person"}, {"org"}),
    "reports_to": ({"person"}, {"person"}),
    "manages":    ({"person"}, {"person", "org", "project"}),
}


def _parse_date_iso(date_str: str) -> str:
    """RFC 2822 or ISO date header -> YYYY-MM-DD. '' on failure."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
        return m.group(1) if m else ""


def _extract_email_addr(header: str) -> str:
    """Bare email from 'Name <email>' or plain 'email'."""
    m = re.search(r"<([^>]+)>", header)
    if m:
        return m.group(1).strip().lower()
    if "@" in header:
        return header.strip().lower().split()[0]
    return ""


def _extract_name(header: str) -> str:
    """Display name from 'Name <email>'."""
    m = re.match(r'^"?([^<"]+)"?\s*<', header)
    if m:
        return m.group(1).strip().strip('"')
    if "@" in header and "<" not in header:
        return ""
    return header.strip()


def _is_owner(name: str, owner: OwnerIdentity) -> bool:
    """True if the name refers to the install owner.

    Single-word aliases match as whole words ("sam" matches "Sam Chen" and
    "sam.c" but not "Samantha"); multi-word aliases match as substrings.
    Word-level matching matters for short configured names: a plain substring
    test would let an alias like "tom" swallow "Tomlinson".
    """
    low = (name or "").lower()
    if not low:
        return False
    tokens = set(re.split(r"[^a-z0-9]+", low))
    for a in owner.aliases:
        if a == low or (" " not in a and a in tokens) or (" " in a and a in low):
            return True
    return False


# ---------------------------------------------------------------------------
# Action lifecycle helpers (ported from enrich_gmail.py / memory_db.py)
# ---------------------------------------------------------------------------

# Subject prefix patterns that signal an intentional self-task
# (enrich_gmail.py:277).
_SELF_PREFIX_RE = re.compile(
    r"^(TODO|Task|Action|Reminder|Follow\s+up|FU)\s*:\s*", re.IGNORECASE)

# Imperative verbs that signal the install owner owns the action when no
# explicit owner is named (enrich_gmail.py:340-348).
_IMPERATIVE_VERBS = frozenset({
    "email", "send", "review", "update", "check", "prepare", "create", "build",
    "investigate", "follow", "confirm", "complete", "schedule", "coordinate", "write",
    "draft", "call", "contact", "add", "remove", "fix", "test", "deploy", "setup",
    "configure", "run", "submit", "request", "provide", "arrange", "register",
    "migrate", "upload", "document", "implement", "organise", "organize", "present",
    "finalise", "finalize", "publish", "share", "order", "book", "ensure", "meet",
    "discuss", "reply", "respond", "escalate", "approve", "sign", "process",
})

# Deadline phrase patterns scanned in action text then body (enrich_gmail.py:145-172).
_DEADLINE_PATTERNS = [
    r'\bby\s+(next\s+)?(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b',
    r'\bby\s+(?:EOD|COB)\b',
    r'\b(?:EOD|COB)\s+(\w+day|tomorrow|today)\b',
    r'\bby\s+end\s+of\s+(?:the\s+)?(?:week|month|day)\b',
    r'\bend\s+of\s+(?:the\s+)?month\b',
    r'\bEOM\b',
    r'\b(?:this|next)\s+week\b',
    r'\btomorrow\b',
    r'\btoday\b',
    r'\bin\s+\d+\s+(?:days?|weeks?)\b',
    r'\bin\s+(?:a|one|two|three|four|five|six|seven)\s+(?:days?|weeks?)\b',
    r'\bnext\s+(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b',
    r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b',
    r'\b\d{4}-\d{2}-\d{2}\b',
    r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?\b',
]

# Baseline confidence for an action from a non-self email with a confirmed
# owner, before owner/deadline inference can lower it. Sits between the
# self-email confidences (1.0/0.85) and the inferred-owner ones (0.6/0.5).
_CONFIRMED_EMAIL_ACTION_CONFIDENCE = 0.7

# Within-batch dedup tokenisation (enrich_gmail.py:1339-1348).
_ACTION_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "with",
                "on", "in", "at", "by"}
_ACTION_PUNCT = re.compile(r"[^\w\s]")

# Near-duplicate fingerprint normalisation (_DEDUP_* constants,
# _normalise_title_for_dedup, _compute_fingerprint) now lives in chunking.py as
# the single source of truth, imported above.


def _norm_action(t: str) -> str:
    """Lowercase, strip punctuation, drop short stopwords (enrich_gmail.py:1342)."""
    s = _ACTION_PUNCT.sub(" ", t.lower())
    return " ".join(w for w in s.split() if w and w not in _ACTION_STOP)


def _infer_owner(is_sender_owner: bool, action_text: str,
                 owner: OwnerIdentity) -> tuple[str, str, float]:
    """Infer the action owner when the LLM returned empty/unclear.

    Ported from enrich_gmail.py:351-366. Returns (owner_name, owner_eid,
    confidence); ('', '', 0.0) when no owner can be assigned.
    """
    if is_sender_owner:
        return (owner.name, owner.entity_id, 0.8)
    text = (action_text or "").strip()
    first_word = text.split()[0].lower().rstrip(".,:") if text else ""
    if first_word in _IMPERATIVE_VERBS:
        return (owner.name, owner.entity_id, 0.6)
    return ("", "", 0.0)


def _infer_deadline(action_text: str, email_body: str, email_date_iso: str) -> str:
    """Parse a deadline from action text or body when the LLM found none.

    Ported from enrich_gmail.py:175-273. Returns an ISO date (YYYY-MM-DD) or ''.
    """
    try:
        import calendar
        from datetime import datetime as _dt, timedelta as _td

        import dateparser

        base_date = None
        if email_date_iso:
            try:
                base_date = _dt.strptime(email_date_iso, "%Y-%m-%d")
            except ValueError:
                pass

        settings = {
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY",
            "RETURN_AS_TIMEZONE_AWARE": False,
        }
        if base_date:
            settings["RELATIVE_BASE"] = base_date

        def _try_parse(phrase: str) -> str:
            p = phrase.strip().lower()
            ref = base_date or _dt.today()

            m_iso = re.match(r"(\d{4})-(\d{2})-(\d{2})$", phrase.strip())
            if m_iso:
                try:
                    return _dt(int(m_iso.group(1)), int(m_iso.group(2)),
                              int(m_iso.group(3))).strftime("%Y-%m-%d")
                except ValueError:
                    pass

            m_dmy = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", phrase.strip())
            if m_dmy:
                try:
                    d, mo, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
                    if y < 100:
                        y += 2000
                    return _dt(y, mo, d).strftime("%Y-%m-%d")
                except ValueError:
                    pass

            if p in ("eom", "end of month", "end of the month",
                     "by end of month", "by end of the month"):
                last_day = calendar.monthrange(ref.year, ref.month)[1]
                return _dt(ref.year, ref.month, last_day).strftime("%Y-%m-%d")

            if p in ("eod", "by eod", "cob", "by cob"):
                return ref.strftime("%Y-%m-%d")

            weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                        "friday": 4, "saturday": 5, "sunday": 6}
            m_next = re.match(r"(?:by\s+)?next\s+(\w+)", p)
            if m_next:
                day_name = m_next.group(1).rstrip(".")
                if day_name in weekdays:
                    target_dow = weekdays[day_name]
                    days_ahead = (target_dow - ref.weekday() + 7) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    return (ref + _td(days=days_ahead)).strftime("%Y-%m-%d")

            m_by = re.match(r"by\s+(\w+day)", p)
            if m_by:
                day_name = m_by.group(1).lower()
                full = {"monday": "monday", "tuesday": "tuesday", "wednesday": "wednesday",
                        "thursday": "thursday", "friday": "friday", "saturday": "saturday",
                        "sunday": "sunday", "mon": "monday", "tue": "tuesday",
                        "wed": "wednesday", "thu": "thursday", "fri": "friday",
                        "sat": "saturday", "sun": "sunday"}
                if day_name in full:
                    phrase = full[day_name]

            parsed = dateparser.parse(phrase, settings=settings)
            return parsed.strftime("%Y-%m-%d") if parsed else ""

        for source in (action_text, (email_body or "")[:2000]):
            if not source:
                continue
            for pat in _DEADLINE_PATTERNS:
                m = re.search(pat, source, re.IGNORECASE)
                if m:
                    result = _try_parse(m.group(0))
                    if result:
                        return result
    except Exception:
        pass
    return ""


def _token_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_self_message(msg: dict, identity: str) -> bool:
    """Phase 1 self-email test: explicit is_self flag, else sender == identity.

    Nexus is_self_email (enrich_gmail.py:369-393) checks sender AND every TO/CC
    recipient against the owner's address list. Phase 1 fixtures carry an explicit
    `is_self` per message, or omit recipients; with no recipient headers we fall
    back to a sender-address match against `identity` (the no-recipient branch of
    is_self_email, which treats a subject-only self-task as self).
    """
    if "is_self" in msg:
        return bool(msg["is_self"])
    sender_addr = _extract_email_addr(msg.get("sender", ""))
    if not sender_addr or sender_addr.lower() != identity.lower():
        return False
    recipient_addrs = []
    for header in (msg.get("to", "") or "", msg.get("cc", "") or ""):
        for part in header.split(","):
            addr = _extract_email_addr(part.strip())
            if addr:
                recipient_addrs.append(addr.lower())
    if not recipient_addrs:
        return True
    return all(a == identity.lower() for a in recipient_addrs)


def _find_near_duplicate_action(conn, text, owner, *, window_days=7,
                                threshold=0.85, today=None) -> int | None:
    """Return the id of an existing OPEN actions row that's a near-duplicate of
    (text, owner) within the last window_days, or None.

    Ported from memory_db.find_near_duplicate_decision (memory_db.py:1780-1856),
    reading the unified `actions` table instead of `decisions`. Fast path: exact
    text_fingerprint match within the window. Slow path: normalised equality,
    token-subset (length-gated), Jaccard, or fuzzy ratio. The Nexus `type` filter
    is dropped (actions has no type column); source is constrained to 'email' to
    mirror the decision-type scope. `today` (an ISO YYYY-MM-DD string) is the
    window baseline; apply() passes the injected clock so the cutoff and the
    actions' created_at are stamped from the same clock (a backfill run with a
    clock far from wall-time then windows correctly). Defaults to UTC today.
    """
    import difflib

    norm = _normalise_title_for_dedup(text)
    if not norm:
        return None

    from datetime import date as _date, timedelta as _timedelta
    baseline = (today or _today())[:10]
    cutoff = (_date.fromisoformat(baseline) - _timedelta(days=window_days)).isoformat()

    fp = _compute_fingerprint(text)
    if fp:
        fp_where = ["status='open'", "source='email'", "text_fingerprint=?",
                    "substr(created_at,1,10) >= ?"]
        fp_params: list = [fp, cutoff]
        if owner:
            fp_where.append("(owner = ? OR owner = '')")
            fp_params.append(owner)
        else:
            fp_where.append("(owner = '' OR owner IS NULL)")
        fp_row = conn.execute(
            f"SELECT id FROM actions WHERE {' AND '.join(fp_where)} LIMIT 1",
            fp_params,
        ).fetchone()
        if fp_row:
            return fp_row[0]

    where = ["status='open'", "source='email'", "substr(created_at,1,10) >= ?"]
    params: list = [cutoff]
    if owner:
        where.append("(owner = ? OR owner = '')")
        params.append(owner)
    else:
        where.append("(owner = '' OR owner IS NULL)")
    sql = (f"SELECT id, text FROM actions WHERE {' AND '.join(where)} "
           "ORDER BY id DESC LIMIT 200")
    for row_id, row_text in conn.execute(sql, params).fetchall():
        cand = _normalise_title_for_dedup(row_text or "")
        if not cand:
            continue
        if cand == norm:
            return row_id
        len_a, len_b = len(norm), len(cand)
        length_ratio = min(len_a, len_b) / max(len_a, len_b) if max(len_a, len_b) else 0.0
        if length_ratio < 0.6:
            continue
        ta, tb = set(norm.split()), set(cand.split())
        if ta and tb and (ta <= tb or tb <= ta):
            return row_id
        if _token_jaccard(norm, cand) >= threshold:
            return row_id
        if difflib.SequenceMatcher(None, norm, cand).ratio() >= 0.82:
            return row_id
    return None


# ---------------------------------------------------------------------------
# apply() — structural write pass (ported from enrich_gmail.py:1043-1600)
# ---------------------------------------------------------------------------

def apply(store, extraction, *, doc_ids, identity=None,
          clock=None, embedder=None, owner=None, home=None, entity_index=None) -> dict:
    """Write one thread's extraction to the graph (structural pass).

    Adapts the Nexus write_to_graph_v2 sequence to mcpbrain: derives the thread
    lead from messages[] (earliest by date), writes the email_context row,
    upserts entities (excluding the install owner), links them to the lead
    message, writes
    role observations, links topics, and writes resolved relations. The action
    LIFECYCLE (owner/deadline inference, age/notification gates, resolve/update)
    is Task 3 — actions are not written here.

    doc_ids: currently unused in this structural pass. Kept as a required kwarg
    because it is the forward seam for Task 3 (action provenance) and Task 5
    (semantic doc linkage) — leave it required so callers wire it through now.

    clock: optional callable returning a `datetime` (the current "now", UTC),
    for deterministic time in tests. Defaults to datetime.now(timezone.utc). It
    drives both the relation valid_from fallback (formatted to YYYY-MM-DD) and
    the action age gate's 60-day boundary.

    embedder: optional embedder (.dim / .embed_passages / .embed_query). The
    semantic layer (Task 5) always writes a synthesised vector doc keyed
    `enriched-{thread_id}` via store.upsert_chunk (embedded=0). When an embedder
    is passed, the doc is embedded inline so it is searchable immediately; when
    None (the daemon's spool path, which does NOT forward an embedder), the doc
    is left unembedded for the daemon's next index_pending pass.

    identity: the synced Gmail address, for self-email detection. None
    resolves config.owner_email (which defaults to the historical address).

    owner: optional OwnerIdentity naming the install owner for action
    attribution and self-exclusion. Defaults to owner_identity_from_config()
    (config.json under MCPBRAIN_HOME). Returns an empty identity when the
    install is not yet configured.

    Returns a small summary dict (counts) for the caller / Task 3. When a thread
    lead exists, a `semantic_doc` key carries the enriched doc_id.
    """
    if identity is None:
        identity = config.owner_email(str(config.app_dir()))
    if owner is None:
        owner = owner_identity_from_config()
    taxonomy = orgs.taxonomy_from_config()
    now = (clock() if clock else datetime.now(timezone.utc))
    today = now.strftime("%Y-%m-%d")

    org = canonical_org(extraction.get("org", "unknown") or "unknown", taxonomy)
    content_type = extraction.get("content_type", "") or ""
    summary = extraction.get("summary", "") or ""
    contextual_summary = extraction.get("contextual_summary", "") or ""
    topics_list = extraction.get("topics", []) or []
    topics_str = ", ".join(topics_list)
    entities_list = extraction.get("entities", []) or []
    messages = extraction.get("messages", []) or []
    thread_id = extraction.get("thread_id", "") or ""

    # Thread lead = earliest message by date. Falls back to first listed.
    lead = None
    if messages:
        lead = min(messages, key=lambda m: m.get("date", "") or "")
    if lead is None:
        # No provenance — nothing structural to anchor on.
        return {"entities": 0, "relations": 0, "topics": 0, "email_context": 0}

    lead_msg_id = lead.get("message_id", "") or ""
    lead_date_iso = _parse_date_iso(lead.get("date", ""))
    sender_header = lead.get("sender", "") or ""
    sender_email = _extract_email_addr(sender_header)
    sender_name = strip_affiliation(_extract_name(sender_header))

    # Sender org precedence:
    #   known org by domain        -> use it;
    #   org_from_email == "external" -> external (a definite "not ours" verdict;
    #                                   do NOT inherit the thread org);
    #   no email signal ("") + known thread org -> thread org;
    #   else                        -> external.
    sender_domain_org = org_from_email(sender_email, taxonomy) if sender_email else ""
    if sender_domain_org and sender_domain_org != "external":
        sender_org = sender_domain_org
    elif sender_domain_org == "external":
        sender_org = "external"
    elif org in taxonomy.names:
        sender_org = org
    else:
        sender_org = "external"

    # Labels: strip Gmail system labels, keep custom ones.
    raw_labels = lead.get("labels", "") or ""
    custom_labels = [
        lbl.strip() for lbl in raw_labels.split(",")
        if lbl.strip() and lbl.strip() not in SYSTEM_LABELS
    ]
    labels_str = ", ".join(custom_labels)

    # Sender entity (skip the owner — self-mail anchors nothing in the graph).
    # Guard against empty identity: "" is a substring of every email address,
    # so the exclusion check must only fire when identity is actually set.
    sender_id = ""
    if sender_name and sender_email and not _is_owner(sender_name, owner) \
            and (not identity or identity.lower() not in sender_email.lower()):
        sender_id = upsert_entity(
            store, name=sender_name, entity_type="person", org=sender_org,
            email_addr=sender_email, taxonomy=taxonomy, valid_from=lead_date_iso) or ""

    # email_context row for the lead message.
    store.upsert_email_context(
        lead_msg_id, subject=lead.get("subject", ""), sender=sender_header,
        sender_email=sender_email, sender_id=sender_id,
        date_str=lead.get("date", ""), date_iso=lead_date_iso,
        thread_id=thread_id, org=org, content_type=content_type,
        summary=summary, topics=topics_str, labels=labels_str,
        contextual_summary=contextual_summary,
        reply_needed=extraction.get("reply_needed", False),
        reply_reason=extraction.get("reply_reason", "") or "")

    linked: set = set()
    name_to_id: dict = {}

    # Link + role for the sender. email_count is driven off the link insert so
    # it counts distinct messages, not apply invocations (idempotent on re-run).
    if sender_id:
        if store.link_email_entity(lead_msg_id, sender_id, role="sender"):
            _bump_email_count(store, sender_id)
        linked.add(sender_id)
        if sender_name:
            name_to_id[sender_name] = sender_id

    entities_created = 0

    # Write-time dedup index. Prefer a caller-supplied index (drain builds ONE per
    # run and reuses it — avoids rebuilding the 25k-entity index on every apply);
    # fall back to building it here for direct callers. Near-duplicates redirect to
    # the existing entity rather than fragmenting the graph.
    _dedup_home = str(home) if home is not None else str(config.app_dir())
    _dedup_enabled = config.write_time_dedup_enabled(_dedup_home)
    _entity_index = entity_index
    if _dedup_enabled and _entity_index is None:
        try:
            _entity_index = build_entity_index(store.entities_for_resolution())
        except Exception:
            log.warning("write_time_dedup: failed to build entity index; skipping dedup this apply()")
            _dedup_enabled = False

    # ── 1. Entities ────────────────────────────────────────────────────────
    for ent in entities_list:
        ename = (ent.get("name") or "").strip()
        etype = (ent.get("type") or "person").strip()
        eorg = (ent.get("org") or "").strip()
        erole = (ent.get("role") or "").strip()

        if etype == "person":
            ename = strip_affiliation(ename)

        if not ename or _is_owner(ename, owner):
            continue
        if etype == "person" and is_junk_entity(ename, "person"):
            continue

        # Q3 write-time dedup: redirect near-duplicates to the existing entity.
        if _dedup_enabled:
            existing_id = write_time_dedup_check(ename, etype, _entity_index)
            if existing_id:
                log.debug("write_time_dedup: %r → %s (existing)", ename, existing_id)
                entity_id = existing_id
                name_to_id[ename] = entity_id
                # Merge: if this mention carries a real org and the existing entity
                # has none, fill it (a redirect shouldn't lose new attribution).
                if eorg and eorg not in orgs.RESERVED_TAGS:
                    try:
                        store.update_entity_org_if_empty(entity_id, eorg)
                    except Exception:  # noqa: BLE001
                        pass
                if entity_id not in linked:
                    if store.link_email_entity(lead_msg_id, entity_id, role="mentioned"):
                        _bump_email_count(store, entity_id)
                    linked.add(entity_id)
                if erole and etype == "person":
                    write_role_observation(store, entity_id, erole, "llm_extraction",
                                           lead_date_iso or "2026-01-01", "medium")
                continue

        entity_id = upsert_entity(store, name=ename, entity_type=etype, org=eorg,
                                  taxonomy=taxonomy, valid_from=lead_date_iso)
        if not entity_id:
            continue
        # Keep the index current so a later entity in THIS batch dedups against
        # the one just created (not just against the store snapshot).
        if _dedup_enabled and _entity_index is not None:
            add_to_index(_entity_index, entity_id, ename, etype)

        name_to_id[ename] = entity_id

        if entity_id not in linked:
            if store.link_email_entity(lead_msg_id, entity_id, role="mentioned"):
                _bump_email_count(store, entity_id)
            linked.add(entity_id)
            entities_created += 1

        if erole and etype == "person":
            write_role_observation(store, entity_id, erole, "llm_extraction",
                                   lead_date_iso or "2026-01-01", "medium")

    # ── 2. Topics with min-distinct-orgs gate (Task 2.6) ───────────────────
    # A topic entity is only created once it has appeared in email_context under
    # at least 2 distinct known orgs. The current row is excluded from the count
    # (exclude_message_id=lead_msg_id below), so only PRIOR appearances open the
    # gate. That is why two distinct orgs need three applies: the first two seed
    # the prior rows, and the third sees them and creates the topic.
    topics_created = 0
    for tag in topics_list:
        tag_clean = (tag or "").strip().lower()
        if len(tag_clean) < 2:
            continue
        if _topic_distinct_orgs(store, tag_clean, exclude_message_id=lead_msg_id) < 2:
            continue
        topic_id = store.upsert_topic_entity(tag_clean)
        if topic_id:
            store.link_email_entity(lead_msg_id, topic_id, role="about")
            topics_created += 1

    # ── 3. Relations ────────────────────────────────────────────────────────
    relations_created = 0
    for rel in extraction.get("relations", []) or []:
        source_name = (rel.get("source_name") or "").strip()
        target_name = (rel.get("target_name") or "").strip()
        rel_type = (rel.get("type") or "").strip()

        if not source_name or not target_name or not rel_type:
            continue
        if rel_type not in VALID_RELATION_TYPES:
            continue
        if _is_owner(source_name, owner) or _is_owner(target_name, owner):
            continue

        # Reject endpoints that are org-classification TAGS, not real entities
        # ("external", "contractor", ...). Real org names that merely contain a
        # tag word ("Acme Corp") are an inexact match and pass through.
        # Tag check runs on the raw name before any stripping.
        if source_name.lower() in taxonomy.org_tags or target_name.lower() in taxonomy.org_tags:
            continue

        def _resolve_endpoint(name, *, for_works_at_target=False):
            """Try the original name first; fall back to the affiliation-stripped
            form only when they differ.  This preserves org names that legitimately
            contain ' at ' or ' from ' (e.g. 'Church at the Bay') while still
            resolving person-affiliation suffixes like 'Franz from The Church Co'."""
            # Primary attempt: resolve the name as given.
            eid = name_to_id.get(name)
            if not eid:
                hit = store.find_entity(name)
                eid = hit["id"] if hit else None
            if eid:
                return eid, name

            # Secondary attempt: try the stripped form if it differs.
            stripped = strip_affiliation(name)
            if stripped != name:
                eid = name_to_id.get(stripped)
                if not eid:
                    hit = store.find_entity(stripped)
                    eid = hit["id"] if hit else None
                if eid:
                    return eid, stripped

            # works_at targets may be orgs not yet seen — upsert with the
            # original name so a real org like "Church at the Bay" is stored intact.
            if for_works_at_target:
                create_name = name  # prefer original for brand-new org entities
                eid = upsert_entity(store, name=create_name,
                                    entity_type="org", org=org, taxonomy=taxonomy)
                if eid:
                    return eid, create_name

            return None, name

        source_id, source_name = _resolve_endpoint(source_name)
        if not source_id:
            continue

        target_id, target_name = _resolve_endpoint(
            target_name, for_works_at_target=(rel_type == "works_at")
        )
        if not target_id:
            continue

        if source_id == target_id:
            continue                                  # self-loop (also caught downstream)

        # Endpoint-type guard: drop e.g. "topic works_at org" or "meeting works_at"
        # — the LLM over-applies the person-centric relations to non-person entities.
        constraint = _RELATION_ENDPOINT_TYPES.get(rel_type)
        if constraint:
            src_ok, tgt_ok = constraint
            s_ent, t_ent = store.get_entity(source_id), store.get_entity(target_id)
            if not s_ent or not t_ent:
                continue
            if (s_ent.get("type") not in src_ok) or (t_ent.get("type") not in tgt_ok):
                continue

        if upsert_relation(store, source_id, rel_type, target_id,
                           valid_from=lead_date_iso or today, evidence=lead_msg_id):
            relations_created += 1

    # ── 4. Action lifecycle (Task 3) ────────────────────────────────────────
    # Runs AFTER the structural pass so owner resolution can use name_to_id and
    # entities written above. is_self / in_inbox come from the thread lead.
    is_self = _is_self_message(lead, identity)
    in_inbox = "INBOX" in [lbl.strip() for lbl in raw_labels.split(",")]
    action_counts = _write_actions(
        store, extraction,
        lead=lead, lead_msg_id=lead_msg_id, lead_date_iso=lead_date_iso,
        content_type=content_type, org=org, sender_id=sender_id,
        sender_name=sender_name, is_self=is_self, in_inbox=in_inbox,
        name_to_id=name_to_id, now=now, thread_id=thread_id, owner=owner)

    # ── 5. Semantic layer (Task 5) ──────────────────────────────────────────
    # One synthesised vector doc per thread, keyed enriched-{thread_id}. Written
    # via upsert_chunk (embedded=0, idempotent on content_hash). When an embedder
    # is injected it is embedded inline; otherwise it is left for the daemon's
    # index_pending pass. Imported here (not at module top) to avoid a
    # semantic -> graph_write -> semantic import cycle.
    from mcpbrain.chunking import content_hash
    from mcpbrain.semantic import build_semantic_doc

    semantic_doc_id = f"enriched-{thread_id}" if thread_id else ""
    if semantic_doc_id:
        semantic_text, semantic_meta = build_semantic_doc(
            extraction, lead, owner=owner, taxonomy=taxonomy)
        store.upsert_chunk(
            doc_id=semantic_doc_id, text=semantic_text,
            content_hash=content_hash(semantic_text), metadata=semantic_meta)
        # The semantic doc is enrichment OUTPUT, not input. Mark it enriched=1 so
        # thread_enrich.group_unenriched_threads never re-picks it as backlog
        # (it carries this thread's thread_id). This leaves embedded untouched:
        # 0 in the deferred path (index_pending still embeds it), 1 after the
        # immediate-embed branch below.
        store.mark_enriched([semantic_doc_id])
        if embedder is not None:
            store.embed_doc(semantic_doc_id, embedder)

    # ── 6. Thread index (thread_context) ────────────────────────────────────
    # Materialise the per-thread record every enriched thread feeds: subject,
    # org, message count, the headline summary, and participant ids. This is the
    # producer the synthesis pass and prepare's prior_thread_context read from;
    # contextual_summary is deliberately left unset here for the deeper synthesis
    # pass to fill (upsert_thread_context preserves a populated one).
    if thread_id:
        store.upsert_thread_context(
            thread_id,
            subject=lead.get("subject", "") if lead else "",
            org=org,
            email_count=len(messages),
            summary=summary,
            participant_ids=",".join(sorted(linked)),
        )

    return {
        "entities": entities_created,
        "relations": relations_created,
        "topics": topics_created,
        "email_context": 1,
        "actions": action_counts["actions"],
        "resolved": action_counts["resolved"],
        "updated": action_counts["updated"],
        "semantic_doc": semantic_doc_id,
    }


def _write_actions(store, extraction, *, lead, lead_msg_id, lead_date_iso,
                   content_type, org, sender_id, sender_name, is_self, in_inbox,
                   name_to_id, now, thread_id, owner) -> dict:
    """Action lifecycle: gates, self-synthesis, dedup, owner/deadline inference,
    routing into the unified actions table, then resolve/update.

    Ported from enrich_gmail.py:1285-1539. The Nexus two-table routing
    (knowledge_actions vs decisions) collapses to a single store.add_unified_action
    call: the not-the-owner / unclear / owner branches differ only in the
    owner/confidence/context_tag written. project_id/area_id pass through from the
    extraction unvalidated (projects/areas tables removed in §9E).

    Gmail label writes (_apply_self_task_label) are intentionally omitted: there
    is no Gmail surface in Phase 1.
    """
    actions_list = extraction.get("actions", []) or []
    body = lead.get("body", "") or ""

    # Stamp actions from the injected clock so the near-duplicate window and the
    # rows' created_at agree (matters for backfills run with a non-wall clock).
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_iso = now_iso[:10]

    # ── Age gate: non-inbox emails more than 60 days old (60 days exactly is
    # exempt) raise no actions. Self-emails bypass (always first-class task
    # sources).
    if not in_inbox and lead_date_iso and not is_self:
        try:
            email_date = datetime.strptime(
                lead_date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if (now - email_date).days > 60:
                actions_list = []
        except Exception:
            pass

    # ── Notification gate: automated emails raise no work. Self-emails bypass
    # (Gemini may misclassify "Reminder:" as a notification).
    if content_type == "notification" and actions_list and not is_self:
        actions_list = []

    # ── Self-email synthetic task: every self-email leaves a task trace.
    if is_self and not actions_list:
        subject_clean = _SELF_PREFIX_RE.sub("", lead.get("subject", "")).strip()
        if not subject_clean:
            subject_clean = "(self-email task - no subject)"
        body_words = body.split()
        if len(body_words) >= 20:
            first_sentence = body.split(".")[0].strip()
            if first_sentence:
                subject_clean = f"{subject_clean}. {first_sentence}"
        actions_list = [{
            "description": subject_clean, "owner_name": owner.name,
            "owner_fallback": "", "due_date": "", "_synthetic": True,
        }]

    # ── Within-batch dedup: drop near-identical actions before any DB write.
    deduped_actions: list = []
    seen_norms: list = []
    for action in actions_list:
        desc = (action.get("description") or "").strip()
        n = _norm_action(desc)
        if any(n == s or n in s or s in n or _token_jaccard(n, s) >= 0.75
               for s in seen_norms):
            continue
        deduped_actions.append(action)
        seen_norms.append(n)
    actions_list = deduped_actions

    is_sender_owner = bool(sender_name and _is_owner(sender_name, owner))

    actions_created = 0
    for action in actions_list:
        description = (action.get("description") or "").strip()
        if not description:
            continue

        owner_name = (action.get("owner_name") or "").strip() or None
        owner_fallback = (action.get("owner_fallback") or "").strip() or None
        due_date = (action.get("due_date") or "").strip()

        resolved_owner_name = owner_name
        resolved_owner_eid = ""

        if owner_name:
            if owner_name.lower() in owner.aliases:
                resolved_owner_eid = owner.entity_id
                resolved_owner_name = owner.name
            elif owner_name.lower() == "unclear":
                resolved_owner_name = None
                owner_name = None
            else:
                hit = store.find_entity(owner_name)
                if hit and hit["type"] == "person":
                    resolved_owner_eid = hit["id"]
                elif owner_name in name_to_id:
                    resolved_owner_eid = name_to_id[owner_name]
        elif owner_fallback == "sender" and sender_id and sender_id != owner.entity_id:
            resolved_owner_eid = sender_id
            resolved_owner_name = sender_name

        context_tag_for_action = ""

        if is_self:
            # Self-email: owner is definitionally the install owner, bypass inference.
            resolved_owner_name = owner.name
            resolved_owner_eid = owner.entity_id
            context_tag_for_action = "self-email"
            synthetic = action.get("_synthetic", False)
            has_prefix = bool(_SELF_PREFIX_RE.match(lead.get("subject", "")))
            action_confidence = (1.0 if has_prefix else 0.85) if synthetic else 1.0
        else:
            action_confidence = _CONFIRMED_EMAIL_ACTION_CONFIDENCE
            if not resolved_owner_name:
                inf_name, inf_eid, inf_conf = _infer_owner(
                    is_sender_owner, description, owner)
                if inf_name:
                    resolved_owner_name = inf_name
                    resolved_owner_eid = inf_eid
                    action_confidence = inf_conf

        # Deadline inference when the LLM returned no deadline.
        deadline_confidence = action_confidence
        if not due_date:
            inferred_d = _infer_deadline(description, body, lead_date_iso)
            if inferred_d:
                due_date = inferred_d
                deadline_confidence = 0.6

        is_not_owner = bool(
            resolved_owner_name
            and resolved_owner_name.lower() not in owner.aliases)

        cluster_id = f"msg-{lead_msg_id[:16]}" if lead_msg_id else ""

        # Single-table routing. The three Nexus branches differ only in the
        # owner / confidence / context_tag written into the same actions table.
        if is_not_owner:
            owner_out = resolved_owner_name or ""
            owner_eid_out = resolved_owner_eid
            confidence_out = action_confidence
        elif not resolved_owner_name:
            owner_out = "unclear"
            owner_eid_out = ""
            confidence_out = 0.5
        else:
            owner_out = resolved_owner_name or ""
            owner_eid_out = resolved_owner_eid
            confidence_out = deadline_confidence

        # Near-duplicate guard: skip insert when an open near-identical row
        # already exists within the 7-day window (windowed on the injected clock).
        with store._connect() as conn:
            if _find_near_duplicate_action(
                    conn, description, owner_out, today=today_iso) is not None:
                continue

        # waiting_on: an action awaiting a reply from a named person. Resolve the
        # awaited entity so the reconciler can match by id, and stamp when the
        # wait began (the email date) so the reconciler's window is anchored.
        waiting_on_name = (action.get("waiting_on") or "").strip()
        waiting_on_eid = ""
        waiting_on_set_at = ""
        if waiting_on_name and not _is_owner(waiting_on_name, owner):
            hit = store.find_entity(waiting_on_name)
            if hit:
                waiting_on_eid = hit["id"]
            elif waiting_on_name in name_to_id:
                waiting_on_eid = name_to_id[waiting_on_name]
            waiting_on_set_at = lead_date_iso or now_iso

        store.add_unified_action(
            text=description, owner=owner_out, owner_entity_id=owner_eid_out,
            status="open", deadline=due_date, org=org,
            project_id=action.get("project_id") or "",
            area_id=action.get("area_id") or "",
            confidence=confidence_out, source="email",
            context_tag=context_tag_for_action, cluster_id=cluster_id,
            source_doc_id=lead_msg_id, thread_id=thread_id,
            text_fingerprint=_compute_fingerprint(description),
            waiting_on=waiting_on_name if waiting_on_name and not _is_owner(waiting_on_name, owner) else "",
            waiting_on_entity_id=waiting_on_eid,
            waiting_on_set_at=waiting_on_set_at,
            created_at=now_iso)
        actions_created += 1

    # ── Close actions resolved by this email. Scoped to this thread's OPEN
    # actions: the extractor only sees this thread's open actions, so a resolve
    # id should never close an already-done action or one from another thread (a
    # hallucinated id then no-ops instead of closing something unrelated).
    resolved_count = 0
    for action_id in (extraction.get("resolved_action_ids") or []):
        if not isinstance(action_id, int) or isinstance(action_id, bool):
            continue
        changed = store.set_action_status(
            action_id, "done", resolved_by=lead_msg_id,
            thread_id=thread_id, only_if_open=True)
        if changed:
            resolved_count += 1
        else:
            log.info("apply: resolved_action_id %s matched no open action in "
                     "thread %s; skipping", action_id, thread_id)

    # ── Update action text where scope/details changed (same thread/open scope).
    updated_count = 0
    for item in (extraction.get("updated_actions") or []):
        if not isinstance(item, dict):
            continue
        action_id = item.get("id")
        new_text = (item.get("new_text") or "").strip()
        if not isinstance(action_id, int) or isinstance(action_id, bool) or not new_text:
            continue
        changed = store.set_action_text(
            action_id, new_text, thread_id=thread_id, only_if_open=True)
        if changed:
            updated_count += 1
        else:
            log.info("apply: updated_action id %s matched no open action in "
                     "thread %s; skipping", action_id, thread_id)

    return {"actions": actions_created, "resolved": resolved_count,
            "updated": updated_count}


# ---------------------------------------------------------------------------
# Topic gate + action-target validation (Task 2.6)
# ---------------------------------------------------------------------------

def _topic_distinct_orgs(store, tag_clean: str, *, exclude_message_id="") -> int:
    r"""Count distinct known orgs whose email_context rows carry this topic.

    Ported from enrich_gmail.py:1256-1272. topics is stored "tag1, tag2"; the
    query strips the spaces then applies a boundary-anchored LIKE so ",budget,"
    matches but "budgeting" does not. The current message is excluded so the
    gate counts only prior appearances (the row is written before this runs).

    The tag is interpolated into a LIKE pattern, so its %, _ and \ metacharacters
    are escaped and an ESCAPE clause is used. Without it a tag like "q1_budget"
    would let the "_" wildcard-match a different topic such as "q1xbudget".
    """
    tag_escaped = (tag_clean.replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
    sql = (
        "SELECT COUNT(DISTINCT org) FROM email_context "
        "WHERE (',' || REPLACE(topics, ', ', ',') || ',') "
        "LIKE '%,' || ? || ',%' ESCAPE '\\' "
        "AND org NOT IN ('', 'unknown')"
    )
    params: tuple = (tag_escaped,)
    if exclude_message_id:
        sql += " AND message_id != ?"
        params = (tag_escaped, exclude_message_id)
    with store._connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Entity writers (ported from memory_db.py:1164-1174, 1561-1572, 1581-1717)
# ---------------------------------------------------------------------------

def _append_alias(conn, entity_id: str, new_alias: str) -> None:
    row = conn.execute(
        "SELECT name, aliases FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if not row:
        return
    existing = [a.strip() for a in (row["aliases"] or "").split(",") if a.strip()]
    seen = {a.lower() for a in existing}
    if row["name"]:
        seen.add(row["name"].lower())
    if new_alias.lower().strip() not in seen:
        existing.append(new_alias.strip())
        conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                     (", ".join(existing), entity_id))


def _bump_email_count(store, entity_id: str) -> None:
    """Increment an entity's email_count by one.

    Called from apply() only when a new (message_id, entity_id) link row was
    created, so email_count tracks distinct message appearances and stays stable
    when a thread is re-applied.
    """
    with store._connect() as conn:
        conn.execute(
            "UPDATE entities SET email_count = email_count + 1 WHERE id = ?",
            (entity_id,))


def _ensure_works_at(conn, entity_id: str, org: str) -> None:
    """Auto-create the org entity and a works_at edge. No-op for external/unknown.

    Uses INSERT OR IGNORE on the legacy UNIQUE(entity_a,relation,entity_b)
    triple — the structural pass; bitemporal works_at supersession runs through
    upsert_relation in the relations loop.
    """
    if not org or org in ("external", "unknown", ""):
        return
    org_id = slugify(org)
    if not org_id:
        return
    today = _today()
    existing_org = conn.execute(
        "SELECT id FROM entities WHERE id = ?", (org_id,)).fetchone()
    if not existing_org:
        conn.execute(
            "INSERT INTO entities (id, name, type, org, first_seen, last_seen) "
            "VALUES (?, ?, 'org', ?, ?, ?)",
            (org_id, org, org, today, today))
    conn.execute(
        "INSERT OR IGNORE INTO entity_relations "
        "(entity_a, relation, entity_b, strength, last_seen) "
        "VALUES (?, 'works_at', ?, 1, ?)",
        (entity_id, org_id, today))


def _set_org_recency(conn, entity_id: str, org: str, valid_from: str) -> None:
    """Write org onto an existing entity, recency-aware. With a valid_from (the
    email date) it overwrites when org is blank OR the stored org_valid_from is
    blank/older — so a job change in a newer email propagates and backfill order
    can't pin a stale org. Without a valid_from it falls back to only-if-blank."""
    if not org:
        return
    if valid_from:
        conn.execute(
            "UPDATE entities SET org = ?, org_valid_from = ? "
            "WHERE id = ? AND (COALESCE(org,'') = '' OR COALESCE(org_valid_from,'') = '' "
            "OR org_valid_from < ?)",
            (org, valid_from, entity_id, valid_from))
    else:
        conn.execute(
            "UPDATE entities SET org = ? WHERE id = ? AND (org = '' OR org IS NULL)",
            (org, entity_id))


def upsert_entity(store, *, name, entity_type, org="", email_addr="",
                  aliases="", notes="", taxonomy=None, valid_from=""):
    """Insert or merge an entity. Returns the surviving entity id, or None.

    Ported from memory_db.py:1581-1717, repointed at store._connect(). Dedup
    order: (1) by email_addr, (2) alias / name
    match, (3) plain id upsert. email_count is bumped only on the email-dedup
    hit (matching Nexus). Title honorifics on person names are stripped and
    recorded as an alias. `valid_from` (the email date) makes org recency-aware —
    a newer-dated observation overwrites a stale org; omit it for only-if-blank.
    """
    if taxonomy is None:
        taxonomy = orgs.taxonomy_from_config()
    org = canonical_org(org, taxonomy)
    name = (name or "").strip()
    if not name:
        return None

    # Canonicalise known org NAMES so every form of a known org converges on one
    # node: "Acme Corp" / "acme corp incorporated" / the "Acme" tag all resolve
    # to the single 'acme' entity. Unknown orgs pass through unchanged. This is
    # what stops the relations loop minting a duplicate node beside the
    # tag-derived node that _ensure_works_at creates.
    if entity_type == "org":
        name = canonical_org(name, taxonomy)

    if is_junk_entity(name, entity_type):
        return None

    title_alias = ""
    if entity_type == "person":
        cleaned, original = strip_title(name)
        if cleaned != original:
            title_alias = original
            name = cleaned

    # (1) email dedup: an existing entity with this email wins, regardless of
    # the candidate display name.
    if email_addr:
        norm_email = email_addr.lower().strip()
        candidate_id = slugify(name)
        with store._connect() as conn:
            existing_by_email = conn.execute(
                "SELECT id FROM entities WHERE lower(email_addr) = ? AND id != ?",
                (norm_email, candidate_id),
            ).fetchone()
            if existing_by_email:
                winner_id = existing_by_email["id"]
                if org:
                    _set_org_recency(conn, winner_id, org, valid_from)
                    if entity_type == "person":
                        _ensure_works_at(conn, winner_id, org)
                # email_count is driven by message links in apply(), not by the
                # upsert itself — re-upserting the same sender on a re-applied
                # thread must not inflate the count.
                conn.execute(
                    "UPDATE entities SET last_seen = ? WHERE id = ?",
                    (_today(), winner_id))
                if title_alias:
                    _append_alias(conn, winner_id, title_alias)
                return winner_id

    eid = slugify(name)
    if not eid:
        return None
    today = _today()

    with store._connect() as conn:
        existing = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()

        if not existing:
            normalised = name.lower().strip()

            # (2) alias / name merge: another entity of the same type whose
            # aliases (or display name) carry this name.
            normalised_original = title_alias.lower().strip() if title_alias else ""
            candidates = conn.execute(
                "SELECT id, aliases FROM entities WHERE aliases != '' AND type = ?",
                (entity_type,)).fetchall()
            alias_matches = []
            for row in candidates:
                alias_list = [a.strip().lower() for a in row["aliases"].split(",") if a.strip()]
                if normalised in alias_list or (normalised_original and normalised_original in alias_list):
                    alias_matches.append(row["id"])

            if not alias_matches:
                name_match = conn.execute(
                    "SELECT id FROM entities WHERE LOWER(name) = ? AND type = ?",
                    (normalised, entity_type)).fetchone()
                if name_match:
                    alias_matches = [name_match["id"]]

            if len(alias_matches) == 1:
                winner_id = alias_matches[0]
                if org:
                    _set_org_recency(conn, winner_id, org, valid_from)
                    if entity_type == "person":
                        _ensure_works_at(conn, winner_id, org)
                conn.execute(
                    "UPDATE entities SET last_seen = ? WHERE id = ?",
                    (today, winner_id))
                return winner_id

        if existing:
            updates: dict = {"last_seen": today}
            if org:
                _existing_ovf = (existing["org_valid_from"]
                                 if "org_valid_from" in existing.keys() else "") or ""
                if not existing["org"]:
                    updates["org"] = org                      # blank → fill
                    if valid_from:
                        updates["org_valid_from"] = valid_from
                elif valid_from and (not _existing_ovf or _existing_ovf < valid_from):
                    updates["org"] = org                      # newer-dated → overwrite stale
                    updates["org_valid_from"] = valid_from
            if email_addr and not existing["email_addr"]:
                updates["email_addr"] = email_addr
            if notes:
                existing_notes = existing["notes"] or ""
                if notes not in existing_notes:
                    updates["notes"] = (existing_notes + "\n" + notes).strip()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE entities SET {set_clause} WHERE id = ?",
                list(updates.values()) + [eid])
            if title_alias:
                _append_alias(conn, eid, title_alias)
            if "org" in updates and entity_type == "person":
                _ensure_works_at(conn, eid, updates["org"])
        else:
            all_aliases = aliases
            if title_alias:
                all_aliases = ", ".join(filter(None, [aliases, title_alias]))
            conn.execute(
                "INSERT INTO entities "
                "(id, name, type, org, org_valid_from, email_addr, aliases, first_seen, last_seen, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, name, entity_type, org, (valid_from if org else ""),
                 email_addr, all_aliases, today, today, notes))
            if org and entity_type == "person":
                _ensure_works_at(conn, eid, org)

    return eid
