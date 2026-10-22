#!/usr/bin/env python3
"""Reversible ingestion clean-up (spec 2026-10-22 §6).

Two entry points share this module and ONE marker (meta key MARKER_KEY), so
whichever runs first wins and the clean-up never runs twice by itself:

* the daemon's one-shot migration (`run_migration`, ruling I13): on every
  install, once, inside the daemon's own process under its bulk lock, gated
  on a backup restore point < 24 h old and the fleet kill switch
  `ingestion_cleanup_enabled`;
* the attended CLI (`mcpbrain clean-ingestion`, or `bin/clean_ingestion.py`).
  Dry run by default (the store is opened READ-ONLY and nothing is written);
  `--yes` applies. Stop the daemon first:

    launchctl bootout gui/$(id -u)/com.mcpbrain
    mcpbrain clean-ingestion                      # dry run: per-step counts
    mcpbrain clean-ingestion --yes                # snapshot, apply, integrity_check
    mcpbrain clean-ingestion --undo dedup_embed --yes
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist

`--yes` (and `--undo ... --yes`) refuses while the daemon answers on its control
port, unless `--store` points at a copy OUTSIDE the app dir. Before writing it
takes a verified snapshot (backup.snapshot: VACUUM INTO + vector probe) and
prints its path; after writing it runs PRAGMA integrity_check and exits 2 (with
the snapshot path) when that is not ok.

Steps run in this order (`--steps a,b` selects a subset, order kept). Order
matters: the source-class archives run before the similarity dedupe, or the
dedupe could keep a derived copy and archive the real one.

  source_session      archive open actions from Claude session notes; cold-mark
                      every session chunk. Repeat captures are NOT superseded:
                      an older capture can hold text the latest lacks (the old
                      24k tail cap), so all stay in recall.
  rekey_notes         chunk-keyed note actions (thread_id `note-<hex>-<n>`) get
                      the base note id; the old value goes to prior_thread_id.
  source_brief        archive brief actions; cold-mark brief chunks.
  source_draft        archive draft actions; a thread whose actions were anchored
                      on its draft but which also holds evidence mail gets that
                      mail re-queued (enriched=0) so the daemon extracts the
                      real commitments; supersede older draft versions per
                      thread (store.supersede_older_drafts); cold-mark drafts.
  source_consolidated archive consolidated-note actions; cold-mark them.
  source_digest       archive digest (`enriched-*`) actions; cold-mark digests.
  derived_links       entity links / relations / observations whose source is a
                      derived class -> derived=1 (relations also strength 0 and
                      their endpoints' degree -1, matching graph_write's derived
                      rows). A relation re-observed since (strength > 1) stays
                      live: later sightings may be evidence. Nothing is deleted.
  dedup_embed         open email actions, single-link clusters at cosine >= 0.88
                      within one logical source (thread_id after rekey_notes),
                      owner-compatible (equal, or either blank/'unclear'); keeps
                      deadline > owner > earliest, archives the rest.
  legacy_strength     relations with no EVIDENCE source row: legacy_strength :=
                      strength where unset (the saved prior; origin untouched).
  sources_backfill    relation_sources / observation_sources from existing
                      provenance (relation source_doc_id, else contrib_doc_id;
                      observation `source` when it names a chunk/message), then
                      strength / observed_count := distinct evidence sources for
                      rows that have one (rows without keep theirs — legacy), and
                      entity email_count := non-derived email_entities rows
                      (local entities). `mentions` is left as is: no row records
                      what was mentioned where, so there is no honest recount.
  source_gone         archive open email actions every identifier of which names
                      a source no longer in the store; orphan links -> derived
                      (store invalidate_sources_for_docs' link step).
  owner_aliases       open actions: owner aliases -> config owner_name;
                      "claude"/"assistant" -> "unclear" (prior in prior_owner).

Every changed value is written to `cleanup_log` (step, table, row key, column,
old and new value as JSON) in the same transaction as the change, one
transaction per step; inserted support rows are logged as inserts. `--undo
STEP` restores the logged values (newest first) of rows that still hold the
value the step wrote, deletes the inserted rows and then those log rows, so a
second undo is a no-op. A row changed since (by the daemon or a later step) is
skipped and reported, its log row kept: UNDO STEPS IN REVERSE STEP ORDER.

`--yes` refuses when no brief rule is configured (ingest subject prefixes /
Drive folders empty and no cached `mcpbrain/generated` label) unless
`--no-brief-rules` is passed; the dry run warns.

Dry-run counts of later steps assume the earlier steps' ACTION changes (it
tracks them in memory); link/relation counts read the current store.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain import config
from mcpbrain import graph_write as gw
from mcpbrain import restore
from mcpbrain import source_class as sc
from mcpbrain.store import _IN_BATCH, Store

SC = sc.SourceClass

STEPS = ("source_session", "rekey_notes", "source_brief", "source_draft",
         "source_consolidated", "source_digest", "derived_links", "dedup_embed",
         "legacy_strength", "sources_backfill", "source_gone", "owner_aliases")

_CLASS_STEPS = {"source_brief": SC.BRIEF, "source_draft": SC.DRAFT,
                "source_consolidated": SC.CONSOLIDATED, "source_digest": SC.DIGEST}
_DERIVED = frozenset({SC.SESSION.value, SC.BRIEF.value, SC.DRAFT.value,
                      SC.CONSOLIDATED.value, SC.DIGEST.value})
_EVIDENCE = frozenset(c.value for c in sc.EVIDENCE)

# Tables a cleanup_log row may name, with their natural key columns (never
# rowid: a rebuild may renumber rowids of tables without an INTEGER PK).
_KEYS = {"actions": ("id",), "chunks": ("doc_id",),
         "email_entities": ("message_id", "entity_id"),
         "entity_relations": ("id",), "entity_observations": ("id",),
         "entities": ("id",), "relation_sources": ("relation_id", "source_key"),
         "observation_sources": ("observation_id", "source_key")}
_INSERT = "*insert*"

SIM_THRESHOLD = gw.ACTION_SIM_THRESHOLD          # 0.88 (global constraint)
_EMBED_BATCH = 256
_NON_PERSON = frozenset(gw._NON_PERSON_OWNERS) - {gw._UNCLEAR_OWNER}
_NOTE_CHUNK = re.compile(r"^(note-[0-9a-f]{32})-\d+$")
_DRIVE_CHUNK = re.compile(r"^gdrive-(.+)-\d+$")
_GMAIL_CHUNK = re.compile(r"^gmail-([0-9a-f]+)-")
_CAL_CHUNK = re.compile(r"^(cal-.+)-\d+$")
# Ids whose shape says which source they name. Anything else (a label, a
# cluster id) is never judged "gone" — unknown is not absent.
_KNOWN_SHAPE = re.compile(
    r"^(gmail-|gdrive-|note-|cal-|anarlog-|enriched-|session-)|^[0-9a-f]{16}$"
    r"|^(?![0-9a-fA-F]{8}-[0-9a-fA-F]{4}-)[A-Za-z0-9_-]{25,}$")   # Drive file id, not a UUID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batched(seq):
    seq = list(seq)
    for i in range(0, len(seq), _IN_BATCH):
        yield seq[i:i + _IN_BATCH]


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

class Resolver:
    """Every chunk's source class and logical source key, plus the identity
    maps actions / links / relations name their source by (message id, file
    id, note id, cal-<event>, anarlog-<session>, Gmail thread id)."""

    def __init__(self):
        self.doc: dict[str, tuple[str, str]] = {}
        self.keys: dict[str, set] = defaultdict(set)
        self.by_class: dict[str, list[str]] = defaultdict(list)
        self.note_of: dict[str, str] = {}
        self.session_notes: dict[str, dict] = {}
        self.draft_threads: set[str] = set()
        # Gmail thread -> its evidence (non-duplicate) chunk doc_ids, and
        # message id -> thread id: for re-queuing a draft-anchored thread.
        self.thread_evidence: dict[str, list[str]] = defaultdict(list)
        self.msg_thread: dict[str, str] = {}

    @classmethod
    def build(cls, db, rules) -> "Resolver":
        self = cls()
        own: dict[str, tuple[str, str]] = {}
        metas: dict[str, dict] = {}
        dups: dict[str, str] = {}
        attachments: list[tuple[str, dict]] = []
        body_cls: dict[str, set] = defaultdict(set)
        for r in db.execute("SELECT rowid, doc_id, metadata, dup_of FROM chunks "
                            "ORDER BY rowid"):
            d = r["doc_id"]
            try:
                meta = json.loads(r["metadata"] or "{}") or {}
            except (TypeError, ValueError):
                meta = {}
            if r["dup_of"]:
                dups[d] = r["dup_of"]
            slim = {k: meta[k] for k in ("source_type", "message_id", "thread_id",
                                         "file_id", "note_id", "event_id",
                                         "session_id") if meta.get(k)}
            metas[d] = slim
            if (meta.get("source_type") == "gmail"
                    and meta.get("content_type") == "email_attachment"):
                attachments.append((d, meta))
                continue
            c = sc.classify(d, {k: v for k, v in meta.items() if k != "dup_of"}, rules)
            own[d] = (c.value, sc.source_key(d, meta))
            if meta.get("source_type") == "gmail" and meta.get("message_id"):
                body_cls[meta["message_id"]].add(c.value)
            if c is SC.SESSION:
                self.session_notes[d] = {
                    "note": meta.get("note_id") or d, "rowid": r["rowid"],
                    "title": str(meta.get("title") or ""),
                    "csid": str(meta.get("claude_session_id") or ""),
                    "at": str(meta.get("captured_at") or meta.get("date") or "")}
            if meta.get("note_id"):
                self.note_of[d] = meta["note_id"]
        # Attachments take their parent message's class (ruling I2). Old rows
        # carry no labels and some old bodies lost DRAFT to the 200-char label
        # clip; with no parent known, an unlabelled attachment stays primary.
        for d, meta in attachments:
            parent = body_cls.get(meta.get("message_id") or "")
            if parent:
                if SC.DRAFT.value in parent:
                    c = SC.DRAFT
                elif SC.BRIEF.value in parent:
                    c = SC.BRIEF
                else:
                    c = SC.PRIMARY_DOC
            elif meta.get("labels"):
                c = sc.classify(d, meta, rules)
            else:
                c = SC.PRIMARY_DOC
            own[d] = (c.value, sc.source_key(d, meta))
        # A duplicate carries its canonical's class and key.
        for d, (c, k) in own.items():
            if d in dups:
                c, k = own.get(dups[d], (SC.DUPLICATE.value, k))
            self.doc[d] = (c, k)
            self.by_class[c].append(d)
            m = metas.get(d, {})
            for ident in self._identities(m):
                self.keys[ident].add((c, k))
            if m.get("source_type") == "gmail" and m.get("thread_id"):
                if c == SC.DRAFT.value:
                    self.draft_threads.add(m["thread_id"])
                if c in _EVIDENCE and d not in dups:
                    self.thread_evidence[m["thread_id"]].append(d)
                if m.get("message_id"):
                    self.msg_thread[m["message_id"]] = m["thread_id"]
        return self

    @staticmethod
    def _identities(m: dict):
        if m.get("source_type") == "gmail":
            if m.get("message_id"):
                yield m["message_id"]
            if m.get("thread_id"):
                yield m["thread_id"]
        if m.get("file_id"):
            yield m["file_id"]
        if m.get("note_id"):
            yield m["note_id"]
        if m.get("session_id"):
            yield f"anarlog-{m['session_id']}"
        if m.get("event_id"):
            yield f"cal-{m['event_id']}"

    @staticmethod
    def _variants(ident: str):
        yield ident
        for pat in (_NOTE_CHUNK, _DRIVE_CHUNK, _GMAIL_CHUNK, _CAL_CHUNK):
            m = pat.match(ident)
            if m:
                yield m.group(1)
        if ident.startswith("cal-") and "_" in ident:       # recurring instance
            yield ident.split("_", 1)[0]

    def resolve(self, ident) -> tuple[str, str] | None:
        """(class, source_key) of the source `ident` names, or None when it is
        unknown or names sources of mixed evidence/derived class."""
        if not ident:
            return None
        if ident in self.doc:
            return self.doc[ident]
        for v in self._variants(ident):
            hits = self.keys.get(v)
            if not hits:
                continue
            classes = {c for c, _ in hits}
            if len(classes) == 1:
                return sorted(hits)[0]
            if classes <= _EVIDENCE:
                return sorted(hits)[0]
            return None
        if ident.startswith("enriched-"):
            return (SC.DIGEST.value, ident)
        if ident.startswith("note-consolidated-"):
            return (SC.CONSOLIDATED.value, ident)
        return None

    def exists(self, ident) -> bool:
        if not ident:
            return False
        if ident in self.doc:
            return True
        if any(v in self.keys for v in self._variants(ident)):
            return True
        if ident.startswith("enriched-"):
            return self.exists(ident[len("enriched-"):])
        return False


# ---------------------------------------------------------------------------
# Logged writes
# ---------------------------------------------------------------------------

class Tx:
    """One step's writes on one connection. In a dry run nothing is executed;
    action changes are mirrored into ctx.actions so later steps see them."""

    def __init__(self, ctx, step, db):
        self.ctx, self.step, self.db = ctx, step, db
        self.counts: Counter = Counter()
        self.at = _now()

    def _log(self, table, key, column, old, new=None):
        ins = column == _INSERT
        self.db.execute(
            "INSERT INTO cleanup_log(step, table_name, row_key, column_name, "
            "old_value, new_value, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.step, table, json.dumps(key, sort_keys=True), column,
             None if ins else json.dumps(old), None if ins else json.dumps(new),
             self.at))

    def update(self, table, key: dict, old: dict, new: dict) -> set:
        changed = {c: v for c, v in new.items() if old.get(c) != v}
        if not changed:
            return set()
        if not self.ctx.dry:
            sets = ", ".join(f"{c}=?" for c in changed)
            where = " AND ".join(f"{k}=?" for k in key)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                            (*changed.values(), *key.values()))
            for c in changed:
                self._log(table, key, c, old.get(c), changed[c])
        if table == "actions" and key["id"] in self.ctx.actions:
            self.ctx.actions[key["id"]].update(changed)
        if table == "entity_relations" and changed.get("derived"):
            self.ctx.derived_rels.add(key["id"])
        old.update(changed)
        return set(changed)

    def insert(self, table, row: dict) -> bool:
        key = {k: row[k] for k in _KEYS[table]}
        if self.ctx.dry:
            where = " AND ".join(f"{k}=?" for k in key)
            return self.db.execute(f"SELECT 1 FROM {table} WHERE {where}",
                                   tuple(key.values())).fetchone() is None
        cols = ", ".join(row)
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES "
            f"({', '.join('?' * len(row))})", tuple(row.values()))
        if cur.rowcount > 0:
            self._log(table, key, _INSERT, None)
            return True
        return False

    def log_diff(self, table, before: dict, after: dict) -> int:
        """Log every column that differs between two {key-tuple: row} reads
        taken around a mutation this step delegated (store helpers)."""
        n = 0
        cols = _KEYS[table]
        for kt, old in before.items():
            new = after.get(kt)
            if new is None:
                continue
            diff = [c for c in old if c not in cols and old[c] != new[c]]
            for c in diff:
                self._log(table, dict(zip(cols, kt)), c, old[c], new[c])
            n += bool(diff)
        return n


def _fetch(db, table, keycol, keys, cols) -> dict:
    out = {}
    for part in _batched(dict.fromkeys(keys)):
        qs = ",".join("?" * len(part))
        for r in db.execute(f"SELECT {keycol}, {', '.join(cols)} FROM {table} "
                            f"WHERE {keycol} IN ({qs})", part):
            out[r[0]] = {c: r[c] for c in cols}
    return out


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, *, home, db, dry, embedder=None):
        self.home, self.dry = home, dry
        self.rules = sc.Rules.from_config(
            home, generated_label_ids=_cached_label_ids(db))
        self.res = Resolver.build(db, self.rules)
        self.owner_name = config.owner_name(home) or ""
        self.owner_aliases = frozenset(a for a in config.owner_aliases(home) if a)
        self._embedder = embedder
        self.derived_rels: set[int] = set()     # marked derived by this run
        self.actions = {r["id"]: dict(r) for r in db.execute(
            "SELECT id, text, owner, owner_entity_id, status, deadline, source, "
            "source_doc_id, thread_id, created_at, resolved_by, resolved_at, "
            "prior_thread_id, prior_owner FROM actions")}

    @property
    def embedder(self):
        if self._embedder is None:
            from mcpbrain.embed import get_embedder
            self._embedder = get_embedder("bge-small")
        return self._embedder

    def open_email_actions(self):
        return [a for a in self.actions.values()
                if a["status"] == "open" and (a["source"] or "email") == "email"]

    def action_class(self, a) -> str | None:
        """The class of the source an action came from: its source_doc_id
        when that resolves, else its thread_id. None = unknown or mixed —
        never archived by a class step."""
        for ident in (a["source_doc_id"], a["thread_id"]):
            hit = self.res.resolve(ident or "")
            if hit is not None:
                return hit[0]
        return None


def _cached_label_ids(db) -> frozenset:
    try:
        r = db.execute("SELECT v FROM meta WHERE k=?",
                       (sc.GENERATED_LABEL_META_KEY,)).fetchone()
        ids = json.loads(r[0]).get("ids") or [] if r and r[0] else []
        return frozenset(str(i) for i in ids if str(i).strip())
    except Exception:  # noqa: BLE001 — no cache means no marker label known
        return frozenset()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _archive_class(ctx, tx, cls) -> list[dict]:
    done = []
    for a in ctx.open_email_actions():
        if ctx.action_class(a) == cls.value:
            tx.update("actions", {"id": a["id"]}, a,
                      {"status": "auto_archived", "resolved_by": tx.step,
                       "resolved_at": tx.at})
            tx.counts[f"actions:{cls.value}"] += 1
            done.append(a)
    return done


def _cold_mark(ctx, tx, doc_ids, label, skip=()) -> None:
    rows = _fetch(tx.db, "chunks", "doc_id", doc_ids, ("enrich_state",))
    for d, row in rows.items():
        if d in skip:
            continue
        if tx.update("chunks", {"doc_id": d}, row, {"enrich_state": "cold"}):
            tx.counts[f"chunks_cold:{label}"] += 1


def step_source_session(ctx, tx):
    """Archive session actions and cold-mark every session chunk. Repeat
    captures are NOT superseded (ruling I11): an older capture can hold text
    the latest lacks (the old 24k tail cap), so all stay in recall, cold."""
    _archive_class(ctx, tx, SC.SESSION)
    _cold_mark(ctx, tx, ctx.res.session_notes, "session")


def step_rekey_notes(ctx, tx):
    for a in ctx.actions.values():
        t = a["thread_id"] or ""
        base = ctx.res.note_of.get(t)
        if not base:
            m = _NOTE_CHUNK.match(t)
            base = m.group(1) if m else ""
        if not base or base == t:
            continue
        prior = a["prior_thread_id"] if a["prior_thread_id"] is not None else t
        tx.update("actions", {"id": a["id"]}, a,
                  {"thread_id": base, "prior_thread_id": prior})
        tx.counts["actions_open" if a["status"] == "open" else "actions_not_open"] += 1


def _step_class(cls):
    def step(ctx, tx):
        archived = _archive_class(ctx, tx, cls)
        skip = set()
        if cls is SC.DRAFT:
            _requeue_draft_threads(ctx, tx, archived)
            skip = _supersede_drafts(ctx, tx)
        _cold_mark(ctx, tx, ctx.res.by_class.get(cls.value, ()), cls.value, skip)
    return step


def _requeue_draft_threads(ctx, tx, archived) -> None:
    """A thread whose actions were anchored on its draft but which also holds
    evidence mail (the sent version, inbound replies) is re-queued: its
    evidence chunks get enriched=0 (logged) so the next daemon cycles extract
    the real mail, now that the draft is no longer an action source."""
    threads = set()
    for a in archived:
        t = a["thread_id"] or ""
        if t not in ctx.res.thread_evidence:
            t = ctx.res.msg_thread.get(a["source_doc_id"] or "", t)
        if ctx.res.thread_evidence.get(t):
            threads.add(t)
    for t in sorted(threads):
        rows = _fetch(tx.db, "chunks", "doc_id", ctx.res.thread_evidence[t], ("enriched",))
        n = 0
        for d, row in rows.items():
            if tx.update("chunks", {"doc_id": d}, row, {"enriched": 0}):
                n += 1
        if n:
            tx.counts["threads_requeued"] += 1
            tx.counts["chunks_requeued:evidence"] += n


def _supersede_drafts(ctx, tx) -> set:
    """store.supersede_older_drafts per thread holding drafts, inside this
    step's transaction (its old values logged)."""
    touched = set()
    for t in sorted(ctx.res.draft_threads):
        ids = Store._supersede_older_drafts_in(tx.db, t, write=False)
        if not ids:
            continue
        if not ctx.dry:
            cols = ("enrich_state", "metadata")
            before = {(k,): v for k, v in _fetch(tx.db, "chunks", "doc_id", ids, cols).items()}
            Store._supersede_older_drafts_in(tx.db, t, write=True)
            after = {(k,): v for k, v in _fetch(tx.db, "chunks", "doc_id", ids, cols).items()}
            tx.log_diff("chunks", before, after)
        touched |= set(ids)
        tx.counts["chunks_superseded:draft_version"] += len(ids)
    return touched


def step_derived_links(ctx, tx):
    db = tx.db
    cache: dict[str, str | None] = {}

    def cls_of(ident):
        if ident not in cache:
            hit = ctx.res.resolve(ident)
            cache[ident] = hit[0] if hit else None
        return cache[ident]

    for r in db.execute("SELECT message_id, entity_id, derived FROM email_entities "
                        "WHERE COALESCE(derived,0)=0").fetchall():
        c = cls_of(r["message_id"])
        if c in _DERIVED:
            tx.update("email_entities", {"message_id": r["message_id"],
                                         "entity_id": r["entity_id"]},
                      {"derived": r["derived"]}, {"derived": 1})
            tx.counts[f"links:{c}"] += 1

    with_evidence = _relations_with_evidence(db)
    degree_dec: Counter = Counter()
    for r in db.execute(
            "SELECT id, entity_a, entity_b, source_doc_id, contrib_doc_id, derived, "
            "strength FROM entity_relations WHERE COALESCE(derived,0)=0 "
            "AND COALESCE(origin,'local')='local' "
            "AND COALESCE(user_verdict,'') <> 'asserted'").fetchall():
        if r["id"] in with_evidence:
            continue
        c = cls_of(r["source_doc_id"] or "") or cls_of(r["contrib_doc_id"] or "")
        if c not in _DERIVED:
            continue
        if (r["strength"] if r["strength"] is not None else 1) > 1:
            # Re-observed since its (derived) first sighting: the later
            # sightings may be evidence, so it stays live (ruling I11).
            # sources_backfill records the derived-class source row, which
            # adds no strength.
            tx.counts[f"relations_kept_reobserved:{c}"] += 1
            continue
        tx.update("entity_relations", {"id": r["id"]},
                  {"derived": r["derived"], "strength": r["strength"]},
                  {"derived": 1, "strength": 0})
        degree_dec[r["entity_a"]] += 1
        degree_dec[r["entity_b"]] += 1
        tx.counts[f"relations:{c}"] += 1
    ents = _fetch(db, "entities", "id", degree_dec, ("degree",))
    for eid, row in ents.items():
        tx.update("entities", {"id": eid}, row,
                  {"degree": max(0, (row["degree"] or 0) - degree_dec[eid])})

    for r in db.execute("SELECT id, source, derived FROM entity_observations "
                        "WHERE COALESCE(derived,0)=0 AND COALESCE(source,'')<>''"
                        ).fetchall():
        c = cls_of(r["source"])
        if c in _DERIVED:
            tx.update("entity_observations", {"id": r["id"]},
                      {"derived": r["derived"]}, {"derived": 1})
            tx.counts[f"observations:{c}"] += 1


def _relations_with_evidence(db) -> set:
    ev = ",".join(f"'{v}'" for v in sorted(_EVIDENCE))
    return {r[0] for r in db.execute(
        f"SELECT DISTINCT relation_id FROM relation_sources WHERE source_class IN ({ev})")}


def _owner_key(ctx, owner) -> str:
    lo = (owner or "").strip().lower()
    if lo in ("", gw._UNCLEAR_OWNER) or lo in _NON_PERSON:
        return ""
    if lo in ctx.owner_aliases and ctx.owner_name:
        return ctx.owner_name.strip().lower()
    return lo


def _unit_vectors(ctx, texts) -> dict:
    vecs = {}
    uniq = list(dict.fromkeys(texts))
    for i in range(0, len(uniq), _EMBED_BATCH):
        part = uniq[i:i + _EMBED_BATCH]
        got = list(ctx.embedder.embed_passages(part))
        if len(got) != len(part):
            raise RuntimeError(f"embedder returned {len(got)} vectors for {len(part)} texts")
        for t, v in zip(part, got):
            vecs[t] = gw._unit(v)
    return vecs


def _clusters(ctx, rows, vecs) -> list[list[dict]]:
    """Single-link clusters at SIM_THRESHOLD, strongest pairs first, never
    joining two different named owners (ruling I6: blank/'unclear' joins any)."""
    n = len(rows)
    try:
        import numpy as np
        m = np.array([vecs[r["text"]] for r in rows], dtype="float32")
        sims = m @ m.T
        hits = np.argwhere(np.triu(sims >= SIM_THRESHOLD, 1))
        pairs = [(float(sims[i, j]), int(i), int(j)) for i, j in hits]
    except ImportError:  # numpy ships with fastembed; this is only a fallback
        pairs = []
        for i in range(n):
            vi = vecs[rows[i]["text"]]
            for j in range(i + 1, n):
                s = sum(a * b for a, b in zip(vi, vecs[rows[j]["text"]]))
                if s >= SIM_THRESHOLD:
                    pairs.append((s, i, j))
    parent = list(range(n))
    owners = [{_owner_key(ctx, r["owner"])} - {""} for r in rows]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _s, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        a, b = find(i), find(j)
        if a == b or len(owners[a] | owners[b]) > 1:
            continue
        parent[b] = a
        owners[a] |= owners[b]
    groups: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[find(i)].append(r)
    return [g for g in groups.values() if len(g) > 1]


def step_dedup_embed(ctx, tx):
    by_source: dict[str, list[dict]] = defaultdict(list)
    for a in ctx.open_email_actions():
        if (a["thread_id"] or "").strip() and (a["text"] or "").strip():
            by_source[a["thread_id"]].append(a)
    groups = [sorted(g, key=lambda a: a["id"]) for g in by_source.values() if len(g) > 1]
    if not groups:
        return
    vecs = _unit_vectors(ctx, [a["text"] for g in groups for a in g])
    for g in groups:
        for cluster in _clusters(ctx, g, vecs):
            keep = min(cluster, key=lambda a: (
                not (a["deadline"] or "").strip(), _owner_key(ctx, a["owner"]) == "",
                a["created_at"] or "", a["id"]))
            for a in cluster:
                if a is keep:
                    continue
                tx.update("actions", {"id": a["id"]}, a,
                          {"status": "auto_archived", "resolved_by": tx.step,
                           "resolved_at": tx.at})
                tx.counts["actions:duplicate"] += 1


def step_legacy_strength(ctx, tx):
    with_evidence = _relations_with_evidence(tx.db)
    for r in tx.db.execute(
            "SELECT id, strength, legacy_strength FROM entity_relations "
            "WHERE legacy_strength IS NULL AND strength IS NOT NULL "
            "AND COALESCE(derived,0)=0").fetchall():
        if r["id"] in with_evidence or r["id"] in ctx.derived_rels:
            continue
        tx.update("entity_relations", {"id": r["id"]},
                  {"legacy_strength": r["legacy_strength"]},
                  {"legacy_strength": r["strength"]})
        tx.counts["relations:legacy"] += 1


def step_sources_backfill(ctx, tx):
    db = tx.db
    # -- relations ----------------------------------------------------------
    support: dict[int, set] = defaultdict(set)
    for r in db.execute("SELECT relation_id, source_key, source_class FROM relation_sources"):
        if r["source_class"] in _EVIDENCE:
            support[r["relation_id"]].add(r["source_key"])
    rels = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, source_doc_id, contrib_doc_id, valid_from, strength, derived, "
        "user_verdict FROM entity_relations WHERE COALESCE(origin,'local')='local'")}
    for rid, r in rels.items():
        hit = (ctx.res.resolve(r["source_doc_id"] or "")
               or ctx.res.resolve(r["contrib_doc_id"] or ""))
        if hit is None:
            continue
        c, key = hit
        if tx.insert("relation_sources", {
                "relation_id": rid, "source_key": key, "source_class": c,
                "first_seen": r["valid_from"] or tx.at}):
            tx.counts[f"relation_sources:{c}"] += 1
            if c in _EVIDENCE:
                support[rid].add(key)
    for rid, keys in support.items():
        r = rels.get(rid)
        if r is None or r["derived"] or rid in ctx.derived_rels:
            continue
        n = len(keys)
        if (r["user_verdict"] or "") == "asserted":
            n = max(n, 1)
        if tx.update("entity_relations", {"id": rid}, {"strength": r["strength"]},
                     {"strength": n}):
            tx.counts["relations:strength"] += 1
    # -- observations -------------------------------------------------------
    osup: dict[int, set] = defaultdict(set)
    for r in db.execute("SELECT observation_id, source_key, source_class "
                        "FROM observation_sources"):
        if r["source_class"] in _EVIDENCE:
            osup[r["observation_id"]].add(r["source_key"])
    obs = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, source, valid_from, observed_count, derived FROM entity_observations")}
    for oid, o in obs.items():
        hit = ctx.res.resolve(o["source"] or "")
        if hit is None:
            continue
        c, key = hit
        if tx.insert("observation_sources", {
                "observation_id": oid, "source_key": key, "source_class": c,
                "first_seen": o["valid_from"] or tx.at}):
            tx.counts[f"observation_sources:{c}"] += 1
            if c in _EVIDENCE:
                osup[oid].add(key)
    for oid, keys in osup.items():
        o = obs.get(oid)
        if o is None or o["derived"]:
            continue
        if tx.update("entity_observations", {"id": oid},
                     {"observed_count": o["observed_count"]},
                     {"observed_count": len(keys)}):
            tx.counts["observations:observed_count"] += 1
    # -- entity email_count (local entities; mentions left as is) -------------
    for r in db.execute(
            "SELECT e.id, e.email_count, "
            "(SELECT COUNT(*) FROM email_entities ee WHERE ee.entity_id=e.id "
            " AND COALESCE(ee.derived,0)=0) AS n "
            "FROM entities e WHERE COALESCE(e.origin,'local')='local'").fetchall():
        if tx.update("entities", {"id": r["id"]}, {"email_count": r["email_count"]},
                     {"email_count": r["n"]}):
            tx.counts["entities:email_count"] += 1


def _gone(ctx, ident) -> bool:
    return bool(ident) and bool(_KNOWN_SHAPE.match(ident)) and not ctx.res.exists(ident)


def step_source_gone(ctx, tx):
    # Actions: archived by exact id, only when EVERY identifier names a known
    # source shape that is absent (a Gmail message deleted from a thread that
    # still lives keeps its actions — Task 9's rule).
    for a in ctx.open_email_actions():
        ids = [x for x in (a["source_doc_id"], a["thread_id"]) if x]
        if ids and all(_gone(ctx, x) for x in ids):
            tx.update("actions", {"id": a["id"]}, a,
                      {"status": "auto_archived", "resolved_by": tx.step,
                       "resolved_at": tx.at})
            tx.counts["actions:source_gone"] += 1
    # Links: store.invalidate_sources_for_docs' link step, in this transaction.
    db = tx.db
    mids = sorted({r[0] for r in db.execute(
        "SELECT DISTINCT message_id FROM email_entities WHERE COALESCE(derived,0)=0")
        if _gone(ctx, r[0])})
    if not mids:
        return
    if ctx.dry:
        for part in _batched(mids):
            qs = ",".join("?" * len(part))
            tx.counts["links:source_gone"] += db.execute(
                f"SELECT COUNT(*) FROM email_entities WHERE message_id IN ({qs}) "
                "AND COALESCE(derived,0)=0", part).fetchone()[0]
        return
    lcols = ("derived", "derived_reason")
    ecols = ("email_count", "mentions")    # removal takes a meeting mention back (M7)

    def links():
        out = {}
        for part in _batched(mids):
            qs = ",".join("?" * len(part))
            for r in db.execute(f"SELECT message_id, entity_id, derived, derived_reason "
                                f"FROM email_entities WHERE message_id IN ({qs})", part):
                out[(r["message_id"], r["entity_id"])] = {c: r[c] for c in lcols}
        return out

    before = links()
    ents = {e for _m, e in before}
    ebefore = {(k,): v for k, v in _fetch(db, "entities", "id", ents, ecols).items()}
    got = Store._invalidate_sources_in(db, mids, reason="source_gone", at=tx.at,
                                       actions=False)
    tx.log_diff("email_entities", before, links())
    tx.log_diff("entities", ebefore,
                {(k,): v for k, v in _fetch(db, "entities", "id", ents, ecols).items()})
    tx.counts["links:source_gone"] += got["links"]


def step_owner_aliases(ctx, tx):
    canon = ctx.owner_name.strip()
    for a in list(ctx.actions.values()):
        if a["status"] != "open":
            continue
        raw = a["owner"] or ""
        lo = raw.strip().lower()
        new = {}
        if lo in _NON_PERSON:
            new = {"owner": gw._UNCLEAR_OWNER, "owner_entity_id": ""}
            label = "actions:non_person"
        elif canon and lo in ctx.owner_aliases and raw != canon:
            new = {"owner": canon}
            label = "actions:owner_alias"
        if not new:
            continue
        new["prior_owner"] = a["prior_owner"] if a["prior_owner"] is not None else raw
        tx.update("actions", {"id": a["id"]}, a, new)
        tx.counts[label] += 1


_STEP_FNS = {
    "source_session": step_source_session,
    "rekey_notes": step_rekey_notes,
    "source_brief": _step_class(SC.BRIEF),
    "source_draft": _step_class(SC.DRAFT),
    "source_consolidated": _step_class(SC.CONSOLIDATED),
    "source_digest": _step_class(SC.DIGEST),
    "derived_links": step_derived_links,
    "dedup_embed": step_dedup_embed,
    "legacy_strength": step_legacy_strength,
    "sources_backfill": step_sources_backfill,
    "source_gone": step_source_gone,
    "owner_aliases": step_owner_aliases,
}


# ---------------------------------------------------------------------------
# Run / undo
# ---------------------------------------------------------------------------

def run(store: Store, home, steps, *, dry: bool, embedder=None) -> dict:
    """Run `steps` (kept in STEPS order). Returns {step: Counter}. A dry run
    reads through one read-only connection; an apply runs one write
    transaction per step."""
    steps = [s for s in STEPS if s in set(steps)]
    out: dict[str, Counter] = {}
    with store._connect() as rdb:
        ctx = Ctx(home=home, db=rdb, dry=dry, embedder=embedder)
        if dry:
            for s in steps:
                tx = Tx(ctx, s, rdb)
                _STEP_FNS[s](ctx, tx)
                out[s] = tx.counts
            return out
    for s in steps:
        with store._connect(write=True) as wdb:
            tx = Tx(ctx, s, wdb)
            _STEP_FNS[s](ctx, tx)
            out[s] = tx.counts
    return out


def undo(store: Store, step: str, *, dry: bool) -> tuple[int, int]:
    """Restore every value `step` logged (newest first) and delete the rows it
    inserted, then delete those log rows. A value is restored only while the
    row still holds the value the step wrote (new_value): a row changed since
    (by the daemon, or by a LATER step — undo steps in reverse step order) is
    left alone, its log row kept, and counted as drifted. Returns
    (restored, drifted); a second undo finds nothing to restore."""
    with store._connect(write=not dry) as db:
        rows = db.execute("SELECT id, table_name, row_key, column_name, old_value, "
                          "new_value FROM cleanup_log WHERE step=? ORDER BY id DESC",
                          (step,)).fetchall()
        if dry:
            return len(rows), 0
        cols_of: dict[str, set] = {}
        done, drifted = [], 0
        for r in rows:
            table = r["table_name"]
            if table not in _KEYS:
                raise ValueError(f"cleanup_log names unknown table {table!r}")
            if table not in cols_of:
                cols_of[table] = {c["name"] for c in db.execute(f"PRAGMA table_info({table})")}
            key = json.loads(r["row_key"])
            if set(key) != set(_KEYS[table]):
                raise ValueError(f"cleanup_log row {r['id']}: bad key {key!r}")
            where = " AND ".join(f"{k}=?" for k in key)
            col = r["column_name"]
            if col == _INSERT:
                db.execute(f"DELETE FROM {table} WHERE {where}",  # admin-delete-ok
                           tuple(key.values()))
                done.append(r["id"])
                continue
            if col not in cols_of[table]:
                raise ValueError(f"cleanup_log row {r['id']}: unknown column {col!r}")
            cur = db.execute(f"SELECT {col} FROM {table} WHERE {where}",
                             tuple(key.values())).fetchone()
            if cur is None:
                done.append(r["id"])               # row gone: nothing to restore
                continue
            if r["new_value"] is not None and cur[0] != json.loads(r["new_value"]):
                drifted += 1
                continue
            db.execute(f"UPDATE {table} SET {col}=? WHERE {where}",
                       (json.loads(r["old_value"]), *key.values()))
            done.append(r["id"])
        for part in _batched(done):
            db.execute(f"DELETE FROM cleanup_log WHERE id IN ({','.join('?' * len(part))})",  # admin-delete-ok
                       part)
        return len(done), drifted


def _print_summary(results: dict, *, dry: bool) -> None:
    print(f"\n[clean] {'DRY RUN — nothing written' if dry else 'APPLIED'}")
    print(f"{'step':<22}{'rows':>9}  by class")
    for step, counts in results.items():
        total = sum(counts.values())
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"
        print(f"{step:<22}{total:>9}  {detail}")


def _daemon_owns(store_path: Path, home: Path) -> bool:
    """True when a running daemon may be writing this store file: it is the
    configured live store (config.store_path()), or lies inside --home."""
    try:
        live = store_path.resolve() == config.store_path().resolve()
    except OSError:
        live = False
    if live and restore.daemon_running(str(config.app_dir())):
        return True
    return _inside(store_path, home) and restore.daemon_running(str(home))


def _inside(path: Path, home: Path) -> bool:
    try:
        path.resolve().relative_to(home.resolve())
        return True
    except ValueError:
        return False


def main(argv=None, *, embedder=None, dim=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=None)
    ap.add_argument("--store", default=None,
                    help="store file (default <home>/brain.sqlite3); a copy outside "
                         "the app dir may be written with the daemon running")
    ap.add_argument("--yes", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--steps", default="", help="comma-separated subset, order kept")
    ap.add_argument("--undo", default=None, metavar="STEP")
    ap.add_argument("--no-brief-rules", action="store_true",
                    help="apply even though no brief rule is configured (briefs "
                         "then read as primary mail/docs)")
    ap.add_argument("--skip-snapshot-for-tests", action="store_true",
                    help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)

    home = Path(ns.home) if ns.home else config.app_dir()
    store_path = Path(ns.store) if ns.store else home / "brain.sqlite3"
    if not store_path.exists():
        print(f"[clean] no store at {store_path}")
        return 1
    steps = [s.strip() for s in ns.steps.split(",") if s.strip()] or list(STEPS)
    bad = [s for s in steps + ([ns.undo] if ns.undo else []) if s not in STEPS]
    if bad:
        print(f"[clean] unknown step(s): {', '.join(bad)}; valid: {', '.join(STEPS)}")
        return 1
    if dim is None:
        from mcpbrain.embed import embedder_dim
        dim = embedder_dim("bge-small")

    write = ns.yes
    if write and _daemon_owns(store_path, home):
        print("[clean] REFUSED: the mcpbrain daemon is running for this home. Stop it "
              "with `launchctl bootout gui/$(id -u)/com.mcpbrain` (not `launchctl "
              "stop`), re-run, then `launchctl bootstrap gui/$(id -u) "
              "~/Library/LaunchAgents/com.mcpbrain.plist`.")
        return 1

    ro = Store(store_path, dim=dim, read_only=True)
    with ro._connect() as db:
        has_schema = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cleanup_log'").fetchone() is not None
        label_ids = _cached_label_ids(db) if has_schema else frozenset()
    rules = sc.Rules.from_config(str(home), generated_label_ids=label_ids)
    no_brief_rules = not (rules.subject_prefixes or rules.drive_folders
                          or rules.generated_label_ids)
    if no_brief_rules and not ns.undo:
        print("[clean] WARNING: NO BRIEF RULE IS CONFIGURED (ingest."
              "generated_gmail_subject_prefixes / ingest.generated_drive_folders "
              "empty, no cached mcpbrain/generated label): briefs will read as "
              "primary mail/docs, keep their actions, and can win the dedupe.")
        if write and not ns.no_brief_rules:
            print("[clean] REFUSED: configure the brief rules, or pass --no-brief-rules.")
            return 1

    if not write:
        store = ro
        if not has_schema:
            print("[clean] store predates the ingestion-integrity schema; run the "
                  "current daemon (or --yes on a copy) once first")
            return 1
        if ns.undo:
            n, _ = undo(store, ns.undo, dry=True)
            print(f"[clean] DRY RUN: --undo {ns.undo} would restore up to {n} logged "
                  "value(s); undo steps in REVERSE step order")
            return 0
        _print_summary(run(store, str(home), steps, dry=True, embedder=embedder), dry=True)
        print("[clean] re-run with --yes to apply")
        return 0

    snap = None
    if not ns.skip_snapshot_for_tests:       # before init(): init writes too
        from mcpbrain.backup import snapshot
        tag = f"pre-undo-{ns.undo}" if ns.undo else "pre-clean"
        snap = snapshot(store_path, Path(f"{store_path}.{tag}-{int(time.time())}"),
                        home=str(home))
        print(f"[clean] verified snapshot: {snap}")
    store = Store(store_path, dim=dim)
    store.init()
    if ns.undo:
        restored, drifted = undo(store, ns.undo, dry=False)
        print(f"[clean] --undo {ns.undo}: restored {restored} logged value(s)")
        if drifted:
            print(f"[clean] --undo {ns.undo}: SKIPPED {drifted} value(s) changed since "
                  "the step ran (kept in cleanup_log); undo later steps first — "
                  "reverse step order")
    else:
        _print_summary(run(store, str(home), steps, dry=False, embedder=embedder), dry=False)
    with store._connect() as db:
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        print(f"[clean] INTEGRITY CHECK FAILED: {ok}")
        if snap:
            print(f"[clean] restore the snapshot (daemon stopped): {snap}")
        return 2
    print("[clean] integrity_check ok")
    return 0
