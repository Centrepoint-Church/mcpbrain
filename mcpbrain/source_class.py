"""One classifier: is a chunk evidence (something that happened) or derived
(something mcpbrain or the owner's automation wrote about it)?

Only evidence may mint or strengthen graph claims. Brief rules are config
(`ingest.*`), never literals here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses
from enum import Enum
from pathlib import Path


class SourceClass(str, Enum):
    PRIMARY_MAIL = "primary_mail"
    PRIMARY_DOC = "primary_doc"
    PRIMARY_EVENT = "primary_event"
    SESSION = "session"
    BRIEF = "brief"
    DRAFT = "draft"
    CONSOLIDATED = "consolidated"
    DIGEST = "digest"
    DUPLICATE = "duplicate"


EVIDENCE = frozenset({
    SourceClass.PRIMARY_MAIL, SourceClass.PRIMARY_DOC, SourceClass.PRIMARY_EVENT,
})

_SESSION_TITLE = re.compile(r"^Session [0-9a-f]{8} \d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Rules:
    owner_emails: frozenset = frozenset()
    subject_prefixes: tuple = ()
    drive_folders: tuple = ()
    generated_label_ids: frozenset = frozenset()

    @classmethod
    def from_config(cls, home, *, generated_label_ids=frozenset()) -> "Rules":
        from mcpbrain import config

        emails = set()
        own = (config.owner_email(home) or "").strip().lower()
        if own:
            emails.add(own)
        try:
            side = (Path(home) / "google_account").read_text().strip().lower()
        except OSError:
            side = ""
        if side:
            emails.add(side)
        return cls(
            owner_emails=frozenset(emails),
            subject_prefixes=tuple(config.generated_gmail_subject_prefixes(home)),
            drive_folders=tuple(config.generated_drive_folders(home)),
            generated_label_ids=frozenset(generated_label_ids),
        )


def is_evidence(cls: SourceClass) -> bool:
    return cls in EVIDENCE


def _addrs(header) -> list[str]:
    return [a.strip().lower() for _, a in getaddresses([str(header or "")]) if a.strip()]


def _label_ids(meta: dict) -> set:
    raw = meta.get("labels") or ""
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return {x.strip() for x in str(raw).split(",") if x.strip()}


def _in_folder(path: str, folders) -> bool:
    p = (path or "").strip().strip("/")
    for f in folders:
        f = f.strip().strip("/")
        if f and (p == f or p.startswith(f + "/")):
            return True
    return False


def _is_session(doc_id: str, meta: dict) -> bool:
    if meta.get("claude_session_id"):
        return True
    tags = {t.strip().lower() for t in str(meta.get("tags") or "").split(",")}
    if "session" in tags:
        return True
    return bool(_SESSION_TITLE.match(str(meta.get("title") or "")))


def classify(doc_id: str, meta: dict, rules: Rules) -> SourceClass:
    meta = meta or {}
    doc_id = doc_id or ""
    if doc_id.startswith("enriched-"):
        return SourceClass.DIGEST
    if meta.get("dup_of"):
        return SourceClass.DUPLICATE
    if doc_id.startswith("note-consolidated-") or meta.get("observation_type") == "consolidated":
        return SourceClass.CONSOLIDATED
    if _is_session(doc_id, meta):
        return SourceClass.SESSION
    st = meta.get("source_type")
    if meta.get("content_type") == "email_attachment":
        return SourceClass.PRIMARY_DOC
    if st == "gmail":
        labels = _label_ids(meta)
        if "DRAFT" in labels:
            return SourceClass.DRAFT
        if labels & rules.generated_label_ids:
            return SourceClass.BRIEF
        subject = str(meta.get("subject") or "")
        if rules.subject_prefixes and subject.startswith(tuple(rules.subject_prefixes)):
            senders = _addrs(meta.get("sender"))
            to = _addrs(meta.get("to"))
            if (senders and to and all(a in rules.owner_emails for a in senders)
                    and all(a in rules.owner_emails for a in to)):
                return SourceClass.BRIEF
        return SourceClass.PRIMARY_MAIL
    if st == "gdrive":
        if str(meta.get("app_generated")) == "1" or _in_folder(
                meta.get("folder_path"), rules.drive_folders):
            return SourceClass.BRIEF
        return SourceClass.PRIMARY_DOC
    if doc_id.startswith("cal-") or meta.get("event_id") or st == "calendar":
        return SourceClass.PRIMARY_EVENT
    return SourceClass.PRIMARY_DOC
