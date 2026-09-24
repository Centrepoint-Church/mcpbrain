"""Entities as MCP resources (mcpbrain://entity/<id>): the daemon side.

The MCP server asks for three things over the control API: the top-N list it
advertises in resources/list, one entity rendered as markdown for
resources/read, and reply-needed ids for draft-reply completion. The list is
cached per UTC day so the MCP server's resource fingerprint (and so
notifications/resources/list_changed) moves about once a day, not on every
degree shift.

No mcp / Store / native imports at module scope.
"""
from datetime import datetime, timezone

TOP_N = 100
RESOURCE_TYPES = ("person", "org", "project")
MAX_RELATIONS = 150
MAX_PER_GROUP = 40
MAX_OBSERVATIONS = 15
_MERGE_HOPS = 10

_cache: dict[tuple[str, str], list[dict]] = {}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _collapse_ws(text: str) -> str:
    """Collapse any run of whitespace (including newlines) to a single space,
    so an entity name containing a stray newline can't break a "# Name"
    header or a "- Name (id)" list line."""
    return " ".join((text or "").split())


def top_entities(store, limit: int = TOP_N, *, today: str | None = None) -> list[dict]:
    path = store._path if hasattr(store, "_path") else store.path
    key = (str(path), today or _today())
    if key not in _cache:
        _cache.clear()  # one day's list at a time
        q = ",".join("?" * len(RESOURCE_TYPES))
        with store._connect() as db:
            rows = db.execute(
                f"SELECT e.id, e.name, e.type, COALESCE(e.org,'') AS org FROM entities e "
                f"LEFT JOIN entity_suppressions s ON s.entity_id = e.id "
                f"WHERE e.type IN ({q}) AND s.entity_id IS NULL "
                f"ORDER BY COALESCE(e.degree,0) DESC, e.id LIMIT ?",
                (*RESOURCE_TYPES, int(limit))).fetchall()
        _cache[key] = [dict(r) for r in rows]
    return list(_cache[key])


def resolve_id(store, entity_id: str) -> tuple[str, str | None] | None:
    with store._connect() as db:
        current, hops = entity_id, 0
        while db.execute("SELECT 1 FROM entities WHERE id=?", (current,)).fetchone() is None:
            row = db.execute("SELECT winner_id FROM entity_merge_log WHERE loser_id=? "
                             "ORDER BY id DESC LIMIT 1", (current,)).fetchone()
            hops += 1
            if row is None or hops > _MERGE_HOPS:
                return None
            current = row[0]
        if db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?",
                      (current,)).fetchone():
            return None
    return current, (entity_id if current != entity_id else None)


def render_markdown(store, entity_id: str) -> dict | None:
    resolved = resolve_id(store, entity_id)
    if resolved is None:
        return None
    eid, merged_from = resolved
    ent = store.get_entity(eid)
    rels = store.relations_for(eid)
    others = store.get_entities({r["entity_b"] if r["entity_a"] == eid else r["entity_a"]
                                 for r in rels})
    with store._connect() as db:
        obs = db.execute(
            "SELECT attribute, value, source, valid_from FROM entity_observations "
            "WHERE entity_id=? AND valid_to IS NULL "
            "AND (invalidated_at IS NULL OR invalidated_at='') AND attribute != 'occurrence' "
            "ORDER BY valid_from DESC LIMIT ?", (eid, MAX_OBSERVATIONS)).fetchall()
    role = next((o["value"] for o in obs if o["attribute"] == "role"), "")
    header = ", ".join(x for x in (ent["type"], ent.get("org") or "") if x)
    lines = [f"# {_collapse_ws(ent['name'])}", "", f"*{header}*" + (f" · {role}" if role else "")]
    if merged_from:
        lines += ["", f"Merged from {merged_from}."]
    if ent.get("email_addr"):
        lines += ["", f"Email: {ent['email_addr']}"]
    groups: dict[str, list[str]] = {}
    for r in rels[:MAX_RELATIONS]:
        out = r["entity_a"] == eid
        other_id = r["entity_b"] if out else r["entity_a"]
        other = others.get(other_id, {})
        label = f"{_collapse_ws(other.get('name', other_id))} ({other_id})"
        groups.setdefault(r["relation"] if out else f"{r['relation']} (from)", []).append(label)
    for rel, labels in sorted(groups.items()):
        lines += ["", f"## {rel}"] + [f"- {x}" for x in labels[:MAX_PER_GROUP]]
        if len(labels) > MAX_PER_GROUP:
            lines.append(f"- … {len(labels) - MAX_PER_GROUP} more")
    if len(rels) > MAX_RELATIONS:
        lines += ["", f"_{len(rels) - MAX_RELATIONS} more relations not shown; "
                      f"use brain_graph for the full neighbourhood._"]
    facts = [o for o in obs if o["attribute"] != "role"]
    if facts:
        lines += ["", "## Recent facts"] + [
            f"- {o['attribute']}: {o['value']} ({o['valid_from'] or 'undated'})" for o in facts]
    return {"id": eid, "markdown": "\n".join(lines) + "\n"}


def reply_needed_ids(store, prefix: str, limit: int = 20) -> list[str]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT message_id FROM email_context WHERE reply_needed=1 AND message_id LIKE ? "
            "ESCAPE '\\' ORDER BY date_iso DESC LIMIT ?",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
             int(limit))).fetchall()
    return [r[0] for r in rows]
