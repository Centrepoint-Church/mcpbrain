"""Gemini-backed extraction of entities / relations / actions / decisions.

The Gemini client is dependency-injected so tests can supply a fake. The real
SDK (`google.genai`) is only imported inside `make_gemini_client`, so importing
this module never requires the SDK to be installed.
"""

import json
import logging
import os
import re

from mcpbrain.chunking import slugify  # re-export: keep `from mcpbrain.enrich import slugify` working
from mcpbrain.chunking import _canonical_name  # re-export: keep `from mcpbrain.enrich import _canonical_name` working

logger = logging.getLogger(__name__)

_EMPTY = {"entities": [], "relations": [], "actions": [], "decisions": []}

# Allowed org affiliations (default-taxonomy view, kept for importers).
# Runtime paths use orgs.taxonomy_from_config().valid_orgs so configured
# installs clamp against their own org list.
from mcpbrain import orgs as _orgs  # noqa: E402 — placed with the enum it owns
_VALID_ORGS = set(_orgs.DEFAULT_TAXONOMY.valid_orgs)

# Allowed thread content types. Single owner: the contract validator imports this
# rather than re-declaring it, so the enrichment enum can't drift from the gate.
_VALID_CONTENT_TYPES = {"request", "update", "decision", "fyi", "notification"}

# Allowed declared entity types. A model-declared type outside this set is
# clamped to "topic". The relation-endpoint STUB type ("unknown") is NOT a
# declared type and is never clamped.
_VALID_TYPES = ("person", "org", "project", "topic")

# Structural junk patterns applied to both person AND org: subject-line prefixes,
# URLs, email addresses, and structural punctuation that never appear in real names.
_STRUCTURAL_JUNK = [
    re.compile(r"^(Re|Fwd|FW|RE|FWD)\s*:", re.IGNORECASE),
    re.compile(r"https?://"),
    re.compile(r"\w+@\w+\.\w+"),
    re.compile(r"[|{}\[\]<>]"),
]

# Numeric junk patterns applied to person ONLY: 4-digit runs (years/amounts) and
# date-like fragments are valid in org/project names (e.g. "OrgName 2026",
# "Vision 2030") but almost always junk in a person name.
_NUMERIC_JUNK = [
    re.compile(r"\d{4}"),
    re.compile(r"\d{2,}/\d{2,}"),
]


def _is_junk_entity(name: str, etype: str) -> bool:
    """Reject obviously-bad person/org entities. Ported from src memory_db.

    person/org are length- and pattern-checked; topic/project are exempt from
    those checks but still must be non-empty (handled by the empty-slug skip).

    Structural patterns (URL, email, subject prefix, brackets) apply to both
    person and org. Numeric patterns (4-digit runs, date fragments) apply to
    person only — org/project names may legitimately contain years.
    """
    if etype not in ("person", "org"):
        return False
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 60:
        return True
    for pattern in _STRUCTURAL_JUNK:
        if pattern.search(name):
            return True
    if etype == "person":
        for pattern in _NUMERIC_JUNK:
            if pattern.search(name):
                return True
    return False

# Current, configurable extraction model. gemini-2.5-flash-lite matches the
# main server's Flash-Lite choice; the old gemini-2.0-flash is 404 for new users.
_DEFAULT_MODEL = os.getenv("MCPBRAIN_ENRICH_MODEL", "gemini-2.5-flash-lite")

# Enrich-mode identifiers written to the store's meta table.
_META_ENRICH_MODE = "enrich_mode"
_MODE_DEFERRED = "deferred"
_MODE_LIVE = "live"


def build_prompt(text: str, metadata: dict) -> str:
    """Compose a deterministic, domain-anchored, strict extraction instruction.

    Returns a prompt asking for strict JSON with a top-level skip boolean plus
    entities/relations/actions/decisions, with light provenance for gmail. The
    rules constrain the model to real named entities and reject junk (dollar
    amounts, newsletter subject lines, form-field labels, etc.).

    The owner's name comes from config (owner_full_name / owner_name) so the
    prompt references the configured identity.
    """
    from mcpbrain import config as _config
    _home = str(_config.app_dir())
    owner_full = _config.owner_full_name(_home)
    owner_short = _config.owner_name(_home)
    owner_role = _config.owner_role(_home)
    _tax = _orgs.taxonomy_from_config(_home)
    if not _tax.names:
        raise ValueError("build_prompt requires at least one org in the taxonomy")
    org_list = ", ".join(_tax.names)
    org_enum = "|".join(list(_tax.names) + ["external", "unknown"])

    prov_lines = []
    if metadata.get("source_type") == "gmail":
        for field in ("subject", "sender", "date"):
            val = metadata.get(field)
            if val:
                prov_lines.append(f"{field}: {val}")
    provenance = ("\n".join(prov_lines) + "\n") if prov_lines else ""

    return (
        f"You extract an operations knowledge graph for {owner_full} "
        f"({owner_role}), who works across these organisations: "
        f"{org_list}.\n\n"
        "Respond with STRICT JSON only. No markdown fences. No commentary. "
        "Use exactly this schema:\n"
        "{\n"
        '  "skip": false,\n'
        '  "entities": [{"name": "...", "type": "person|org|project|topic", '
        f'"org": "{org_enum}"}}],\n'
        '  "relations": [{"from": "<entity name>", "relation": '
        '"works_at|reports_to|manages|coordinates_with|mentioned_with", '
        '"to": "<entity name>"}],\n'
        '  "actions": [{"text": "...", "owner": "<canonical person name or empty>", '
        '"deadline": "<ISO date or empty>"}],\n'
        '  "decisions": [{"text": "...", "decided_on": "<ISO date or empty>"}]\n'
        "}\n\n"
        "Rules:\n\n"
        "skip:\n"
        "  - true ONLY for automated/system/newsletter/marketing/promotional content: "
        "delivery receipts, bounces, mailing-list or newsletter blasts, marketing.\n"
        "  - false for human-authored ministry/operations content, including FYIs.\n\n"
        "entities:\n"
        "  - Only REAL named people, organisations, projects, or genuine topics.\n"
        f"  - EXCLUDE {owner_full}.\n"
        "  - name: full canonical form (\"Joel Chelliah\" not \"Pastor Joel\"); no honorifics.\n"
        "  - type must be one of: person|org|project|topic.\n"
        f"  - org must be one of: {org_enum}. "
        "It is a person/org's affiliation, NOT arbitrary text (never a word like \"WORSHIP\").\n"
        "  - DO NOT extract as entities: dollar amounts (\"$0\", \"$300\"), "
        "article/newsletter/email subject lines, product or software names, generic words, "
        "UI/form/slide labels, URLs, email addresses, dates.\n\n"
        "relations:\n"
        "  - Only between entities listed in entities[].\n"
        "  - relation must be one of: works_at|reports_to|manages|coordinates_with|mentioned_with.\n"
        f"  - Never include {owner_full}.\n\n"
        "actions:\n"
        f"  - Real tasks {owner_short} must act on, stated specifically.\n"
        "  - NOT form-field labels, slide headings, or single interrogatives "
        "(\"What?\", \"Why?\", \"Who?\").\n"
        "  - owner = canonical person name or empty. At most 5 per document.\n\n"
        "decisions:\n"
        "  - Real agreements or approvals, stated as a sentence. Not labels or fragments.\n\n"
        f"{provenance}"
        "Document:\n"
        f"{text}"
    )


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        # drop opening fence (optionally ```json) and trailing fence
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_first_json_object(raw: str) -> dict:
    """Parse the first complete JSON OBJECT from raw, ignoring any trailing
    content (the model occasionally appends a second object or trailing text
    despite response_mime_type=json). Scans each '{' in turn so an inline brace
    in leading prose (e.g. "use {x} then {...}") doesn't abort the parse.
    Raises ValueError if no decodable object is found."""
    s = _strip_fences(raw)
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        start = s.find("{", pos)
        if start == -1:
            raise ValueError("no JSON object in model output")
        try:
            obj, _end = decoder.raw_decode(s[start:])
        except json.JSONDecodeError:
            pos = start + 1  # this brace didn't start a valid value; try the next
            continue
        if isinstance(obj, dict):
            return obj
        pos = start + 1  # decoded a non-dict value here; keep scanning


def extract(client, text: str, metadata: dict, model: str | None = None) -> dict:
    """Call the injected Gemini client, parse strict JSON, return the four lists.

    API/transport failures (404 model error, quota, network) from the
    generate_content CALL PROPAGATE — they are transient/fixable and must not be
    swallowed, or chunks get marked enriched with zero extraction and never
    retried. Unparseable model OUTPUT, and a safety-blocked response (where
    resp.text raises), are PERMANENT properties of that content, so they return
    all-empty lists (and the chunk is marked done) rather than re-queuing forever.
    """
    model = model or _DEFAULT_MODEL
    prompt = build_prompt(text, metadata)
    # response_mime_type forces JSON output (matches the main server). API/transport
    # errors from this CALL propagate; resp.text + parse below are caught.
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    try:
        # resp.text can RAISE on a safety-blocked response (no text part) — treat
        # that like unparseable output (permanent), not a transient API error.
        raw = resp.text or ""
        data = _parse_first_json_object(raw)
    except Exception as exc:  # blocked / bad MODEL OUTPUT (not an API failure) -> legitimately empty
        logger.warning("enrich.extract: unparseable or blocked model output: %s", exc)
        return {k: [] for k in _EMPTY}

    # skip is a PERMANENT decision for automated/newsletter content: return all
    # empty so the caller marks the chunk enriched (done), not retried.
    if data.get("skip"):
        return {k: [] for k in _EMPTY}

    return {
        "entities": data.get("entities") or [],
        "relations": data.get("relations") or [],
        "actions": data.get("actions") or [],
        "decisions": data.get("decisions") or [],
    }


def enrich_document(store, client, doc_id: str, text: str, metadata: dict,
                    model: str | None = None) -> dict:
    """Extract from one document and write rows into the graph tables.

    Returns a summary dict of counts written. Propagates any API/transport
    error raised by extract().
    """
    model = model or _DEFAULT_MODEL
    result = extract(client, text, metadata, model=model)
    seen = metadata.get("date", "")
    thread_id = metadata.get("thread_id", "")

    n_entities = n_relations = n_actions = n_decisions = 0

    declared = set()  # slugs explicitly present in entities[]
    for ent in result["entities"]:
        name = _canonical_name(ent.get("name"))
        # Clamp the model-declared type to the allowed enum; anything else -> topic.
        etype = ent.get("type", "topic")
        etype = etype if etype in _VALID_TYPES else "topic"
        slug = slugify(name)
        if not slug:
            continue
        if _is_junk_entity(name, etype):
            continue
        # Clamp org to the allowed enum so arbitrary text (e.g. "WORSHIP") can't land.
        org = ent.get("org", "")
        if org not in _orgs.taxonomy_from_config().valid_orgs:
            org = ""
        if store.upsert_entity(slug, name, etype, org, seen=seen):
            n_entities += 1  # count only newly created entity rows
        declared.add(slug)

    for rel in result["relations"]:
        from_name = _canonical_name(rel.get("from"))
        to_name = _canonical_name(rel.get("to"))
        a = slugify(from_name)
        b = slugify(to_name)
        relation = rel.get("relation", "")
        if not (a and b and relation):
            continue
        # endpoints carry no type; screen with "org" (structural patterns only) to avoid
        # over-rejecting year-bearing org/project names (e.g. "OrgName 2026").
        if _is_junk_entity(from_name, "org") or _is_junk_entity(to_name, "org"):
            continue
        # stub any endpoint not declared above so there's no dangling edge
        for endpoint, raw_name in ((a, from_name), (b, to_name)):
            if endpoint not in declared:
                if store.upsert_entity(endpoint, raw_name, "unknown", "", seen=seen):
                    n_entities += 1  # count a stub only if it created a new entity
                declared.add(endpoint)
        if store.add_relation(a, relation, b, source_doc_id=doc_id):
            n_relations += 1  # count only newly inserted (non-duplicate) relations

    for act in result["actions"]:
        atext = act.get("text", "")
        if not atext:
            continue
        store.add_action(atext, owner=act.get("owner", ""),
                         deadline=act.get("deadline", ""),
                         source_doc_id=doc_id, thread_id=thread_id)
        n_actions += 1

    for dec in result["decisions"]:
        dtext = dec.get("text", "")
        if not dtext:
            continue
        store.add_decision(dtext, decided_on=dec.get("decided_on", ""),
                           source_doc_id=doc_id)
        n_decisions += 1

    return {
        "entities": n_entities,
        "relations": n_relations,
        "actions": n_actions,
        "decisions": n_decisions,
    }


def run_enrichment(store, docs, client=None) -> dict:
    """Enrich a batch of docs into the graph, or defer if no client.

    docs: iterable of (doc_id, text, metadata) tuples.
    client: an injected Gemini client, or None.

    If client is None -> DEFER: write NO graph rows, set store meta
        'enrich_mode' = 'deferred', return {"mode": "deferred", ...zero counts}.
    If client is provided -> LIVE: set meta 'enrich_mode' = 'live', call
        enrich_document for each doc, accumulate written-row counts, return
        {"mode": "live", entities, relations, actions, decisions}.
    """
    if client is None:
        store.set_meta(_META_ENRICH_MODE, _MODE_DEFERRED)
        return {"mode": _MODE_DEFERRED, "entities": 0, "relations": 0,
                "actions": 0, "decisions": 0, "errors": 0}

    store.set_meta(_META_ENRICH_MODE, _MODE_LIVE)
    totals = {"entities": 0, "relations": 0, "actions": 0, "decisions": 0}
    processed, errors = [], 0
    for doc_id, text, metadata in docs:
        try:
            counts = enrich_document(store, client, doc_id, text, metadata)
        except Exception as exc:
            # An API/transport error (404, quota, network) must NOT mark the doc
            # enriched. Leave enriched=0 so it retries next run; carry on to the
            # next doc rather than aborting the whole batch.
            logger.warning("enrich: skipping %s (will retry next run): %s", doc_id, exc)
            errors += 1
            continue
        for key in totals:
            totals[key] += counts[key]
        processed.append(doc_id)
    # Mark only the docs that succeeded so they aren't re-enriched next cycle.
    # Defer path (client None) deliberately skips this: enriched stays 0 so the
    # chunks enrich once a key appears.
    store.mark_enriched(processed)
    return {"mode": "live", **totals, "errors": errors}


def resolve_client(api_key: str | None):
    """Return a live Gemini client if api_key is non-empty, else None (defer).

    Falsy values (None or '') -> return None without importing the SDK.
    Any non-empty string -> attempt to construct a live client.
    """
    if api_key is not None and api_key != "":
        return make_gemini_client(api_key)
    return None


def make_gemini_client(api_key: str):
    """Construct a real Gemini client. SDK imported lazily so module import is free."""
    from google import genai
    return genai.Client(api_key=api_key)
