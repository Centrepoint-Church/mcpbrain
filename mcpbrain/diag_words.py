"""Plain-English wording for doctor's checks, for the console's Diagnostics.

doctor writes for a terminal: a status glyph, a padded check name, then
detail with paths, counts, exception text and CLI advice. The console must
not show any of that raw. Each check doctor reports has one entry here that
turns its status and detail into a sentence a person can act on. A check
with no entry, or a detail an entry does not recognise, gets a safe generic
sentence, never the raw text (that stays in `text` for the support report).

tests/test_diag_words.py runs doctor's own line builders through this table
and fails when one of them falls through to the generic sentence, so a new
check cannot ship unworded.
"""
from __future__ import annotations

import re
from typing import Callable

LOG = "Details are in the mcpbrain log."
HELP = "Ask for help if it doesn't clear."
RECONNECT = "Use Reconnect and restart under Connections."

_GLYPHS = (("✅", "ok"), ("⚠️", "warn"), ("⚠", "warn"), ("❌", "bad"),
           ("➖", "info"), ("⏳", "info"), ("ℹ️", "info"), ("ℹ", "info"))

# Checks doctor names in a padded label column, plus three it writes as
# "<name>: <count>". Longest first so "Sync failures" never matches "Sync".
_LABELS = sorted((
    "Daemon", "Records", "Google", "Enrichment", "Backup", "Embedder",
    "Baseline", "Watchdog", "Restore", "Architecture", "Tenant", "Connector",
    "MCP version", "OCR", "Chunk window", "Repair state", "Sync queue",
    "Sync failures", "Infra drives", "Invalidators", "Integrity", "Reflow",
    "Clean-up", "Scheduled tasks", "content-free chunks",
    "Items awaiting re-chunk", "dangling invalidator pointers",
), key=len, reverse=True)

NAMES = {
    "Daemon": "Background service", "Records": "Records folder",
    "Google": "Google", "Enrichment": "Enrichment", "Backup": "Backup",
    "Embedder": "Search model", "Baseline": "Organisation snapshot",
    "Watchdog": "Stall recovery", "Restore": "Restore", "Architecture": "Processor",
    "Tenant": "Organisation", "Connector": "Claude connection",
    "MCP version": "Claude sessions", "OCR": "Scanned PDFs",
    "Chunk window": "Long documents", "Repair state": "Index repair",
    "Sync queue": "Sync", "Sync failures": "Sync", "Infra drives": "Shared drives",
    "Invalidators": "Record links", "Integrity": "Database",
    "Reflow": "Re-processing", "Clean-up": "Action clean-up",
    "Scheduled tasks": "Scheduled tasks", "content-free chunks": "Search index",
    "Items awaiting re-chunk": "Search index",
    "dangling invalidator pointers": "Record links",
}


def _num(pattern: str, detail: str) -> int | None:
    m = re.search(pattern, detail)
    return int(m.group(1).replace(",", "")) if m else None


def _items(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _daemon(st, d):
    if st == "ok":
        return "The background service is running."
    if st == "warn":
        return "Claude isn't connected to mcpbrain yet. Install the mcpbrain plugin in Claude."
    return "The background service isn't running. Restart mcpbrain from the menu bar. " + LOG


def _records(st, d):
    if st == "ok":
        return "Your records folder is ready."
    return "Your records folder couldn't be set up. " + LOG


def _google(st, d):
    if st == "ok":
        return "Google is connected."
    if "unreachable" in d:
        return ("mcpbrain can't reach Google right now and is retrying on its own. "
                "Sync and backup wait until it's back.")
    return "Google needs you to sign in again. Use Reconnect under Connections."


def _enrichment(st, d):
    if st == "ok":
        return "Enrichment is running."
    if st == "info":
        return "Enrichment isn't set up. It's optional."
    return "Enrichment has stalled. Open Claude so its scheduled tasks can run."


def _backup(st, d):
    if st == "ok":
        if "restore point" not in d:
            return "Backups are working."
        return "Backups are working: " + d.replace(" · ", ", ").rstrip(".") + "."
    if st == "info":
        return "Backup isn't set up. It's optional."
    if "damaged local backup" in d:
        return ("A damaged backup on this computer was set aside and a fresh full "
                "backup started. Nothing to do.")
    n = _num(r"(\d[\d,]*) old backup file", d)
    if n is not None:
        return (f"{_items(n, 'old backup file')} couldn't be removed from Drive. "
                "Check the backup folder's sharing permissions.")
    if "old snapshots kept" in d:
        return ("Older backups are being kept because the newest is much smaller "
                "than usual. Nothing to do unless this persists.")
    return "Backups need attention. Check Encrypted backup under Connections."


def _embedder(st, d):
    if st == "ok":
        return "The search model is on this computer."
    return ("The search model is missing and couldn't be downloaded. "
            "Run Diagnostics again when you're online.")


def _baseline(st, d):
    if st == "ok":
        return "The shared organisation snapshot is loaded."
    if st == "bad":
        return "Loading the shared organisation snapshot failed. " + LOG
    if "not checked" in d:
        return "The shared organisation snapshot isn't checked from here."
    return "The shared organisation snapshot isn't available yet. It loads on its own."


def _watchdog(st, d):
    if st == "bad":
        return ("mcpbrain stalled repeatedly and has stopped restarting itself. "
                "Restart it from the menu bar. " + LOG)
    n = _num(r"(\d+) self-restart", d)
    if n:
        return (f"mcpbrain restarted itself {_items(n, 'time')} in the last 6 hours "
                "and recovered.")
    return "mcpbrain hasn't stalled."


def _restore(st, d):
    if st == "bad":
        return ("Restoring from backup failed, so mcpbrain started without it. "
                "Ask for help before changing anything.")
    return ("A restore from backup is waiting for Google. Restart mcpbrain from the "
            "menu bar while you're online.")


def _architecture(st, d):
    if st == "ok":
        return "mcpbrain matches this computer's processor."
    return "mcpbrain was installed for a different processor than this computer's. Reinstall it."


def _tenant(st, d):
    if st == "ok":
        name = re.sub(r"\s*\([^)]*\)\s*$", "", d).strip()
        return f"Set up for {name}." if name else "Set up for your organisation."
    return ("This copy isn't set up for an organisation, so backup upload and "
            "updates are off. Ask for help.")


def _connector(st, d):
    app = ("Claude Code" if ".claude.json" in d
           else "Claude Desktop" if "claude_desktop_config" in d else "Claude")
    if st == "ok":
        return f"{app} is connected to mcpbrain."
    if st == "info":
        return f"{app} isn't on this computer, or hasn't been opened yet."
    return f"{app} isn't connected to mcpbrain. " + RECONNECT


def _mcp_version(st, d):
    n = _num(r"(\d+) live server", d) or 0
    who = "One open Claude session is" if n == 1 else f"{n} open Claude sessions are"
    return (f"{who} still using an older mcpbrain. Quit Claude Desktop completely "
            "and close any long-running Claude Code sessions.")


def _ocr(st, d):
    if st == "ok":
        return "Scanned PDFs can be read."
    return "Scanned PDFs can't be read because the text reader is missing. Ask for help installing it."


def _chunk_window(st, d):
    if st == "ok":
        return "Long documents are fully searchable."
    n = _num(r"\((\d[\d,]*) left\)", d)
    left = f" ({_items(n, 'document')} left)" if n else ""
    return f"Some long documents are still being re-indexed{left}. This finishes on its own."


def _sync_queue(st, d):
    n = _num(r"(\d[\d,]*) pending", d) or 0
    if not n:
        return "Nothing waiting to sync."
    age = re.search(r"oldest (.+?) old", d)
    if st == "ok":
        return f"{_items(n, 'new item')} being fetched now."
    since = f" for {age.group(1)}" if age else ""
    return f"{_items(n, 'item')} waiting to sync{since}. {LOG}"


def _sync_failures(st, d):
    n = _num(r"(\d+) items? retrying", d) or 0
    return f"{_items(n, 'item')} keep failing to sync and are being retried. {LOG}"


def _infra(st, d):
    return ("mcpbrain left some files in a shared drive because they look like real "
            "documents. Keep mcpbrain's backup folders in a Shared Drive of their own.")


def _integrity(st, d):
    if st == "ok":
        return "The database passed its integrity check."
    return "The database failed its integrity check. Don't restore or reinstall; ask for help first."


def _reflow(st, d):
    if st == "ok":
        return "Background re-processing is finished."
    if st == "bad":
        return "Background re-processing stopped after finding a problem. Ask for help."
    if "no_recent_backup" in d:
        return "Background re-processing is paused until the next backup runs."
    held = _num(r"(\d+) owner\(s\) held", d)
    if held:
        return (f"Background re-processing can't finish {_items(held, 'item')} and "
                "retries them once a day. " + HELP)
    if st == "warn":
        return "Background re-processing is paused. " + LOG
    n = _num(r"^(\d[\d,]*) queued", d)
    queued = f" ({n:,} queued)" if n else ""
    return f"Background re-processing is under way{queued}. This finishes on its own."


def _cleanup(st, d):
    if st == "ok":
        return "The one-off clean-up of old actions is done."
    if st == "bad":
        return "The one-off clean-up of old actions failed. " + LOG
    if st == "warn":
        return "The one-off clean-up of old actions needs a look. " + LOG
    return "The one-off clean-up of old actions is waiting to run."


def _scheduled(st, d):
    if st == "ok":
        return "Claude's scheduled tasks are running."
    return ("Claude's scheduled tasks may not be running. In Claude Code, run "
            "/mcpbrain:install to set them up again.")


def _content_free(st, d):
    n = _num(r"(\d[\d,]*)", d) or 0
    if not n:
        return "No empty entries in the search index."
    return f"{_items(n, 'search entry', 'search entries')} have no text. " + HELP


def _rechunk(st, d):
    n = _num(r"(\d[\d,]*)", d) or 0
    if not n:
        return "No documents waiting to be re-indexed."
    return f"{_items(n, 'document')} waiting to be re-indexed. This finishes on its own."


def _dangling(st, d):
    n = _num(r"(\d[\d,]*)", d) or 0
    if not n:
        return "Links between records are intact."
    return f"{_items(n, 'link')} between records point at nothing. Ask for help."


WORDS: dict[str, Callable[[str, str], str | None]] = {
    "Daemon": _daemon, "Records": _records, "Google": _google,
    "Enrichment": _enrichment, "Backup": _backup, "Embedder": _embedder,
    "Baseline": _baseline, "Watchdog": _watchdog, "Restore": _restore,
    "Architecture": _architecture, "Tenant": _tenant, "Connector": _connector,
    "MCP version": _mcp_version, "OCR": _ocr, "Chunk window": _chunk_window,
    "Sync queue": _sync_queue, "Sync failures": _sync_failures,
    "Infra drives": _infra, "Integrity": _integrity, "Reflow": _reflow,
    "Clean-up": _cleanup, "Scheduled tasks": _scheduled,
    "content-free chunks": _content_free, "Items awaiting re-chunk": _rechunk,
    "dangling invalidator pointers": _dangling,
}


def generic(name: str, status: str) -> str:
    """The sentence for a check this table cannot word. Never the raw text."""
    if status == "ok":
        return f"{name}: OK."
    if status == "info":
        return f"{name}: nothing to do."
    return f"{name}: this needs a look. {LOG}"


def split(line: str) -> tuple[str, str, str] | None:
    """(status, check, detail) for one doctor line; None for its header,
    summary and blank lines, which the console does not show."""
    s = line.strip()
    if not s or s.startswith("mcpbrain doctor") or re.match(r"^\d+ fixed automatically", s):
        return None
    status = "info"
    for glyph, st in _GLYPHS:
        if s.startswith(glyph):
            status, s = st, s[len(glyph):].lstrip("️").strip()
            break
    for label in _LABELS:
        if s.startswith(label):
            return status, label, s[len(label):].lstrip(": ").strip()
    return status, "", s


def word(line: str) -> dict | None:
    """One doctor line as {check, name, status, words}; None when hidden."""
    parts = split(line)
    if parts is None:
        return None
    status, check, detail = parts
    name = NAMES.get(check, "One check")
    if re.search(r"\.\.\.\s*✅\s*fixed\s*$", detail):
        return {"check": check, "name": name, "status": "ok",
                "words": f"{name} had a problem and was fixed automatically.",
                "worded": True}
    if re.match(r"skipped\b", detail):
        return {"check": check, "name": name, "status": "info",
                "words": f"{name} wasn't checked this time. {LOG}", "worded": True}
    text = None
    fn = WORDS.get(check)
    if fn is not None:
        try:
            text = fn(status, detail)
        except Exception:  # noqa: BLE001 — wording must never break Diagnostics
            text = None
    return {"check": check, "name": name, "status": status,
            "words": text or generic(name, status), "worded": text is not None}
