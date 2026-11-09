"""The console's Settings writes: an allowlist of keys, type-checked, merged one
level deep into config.json.

/api/config replaces a nested block wholesale and accepts any key; this does
neither. A body naming any key outside the allowlist, or a value of the wrong
type, is refused whole and config.json is left untouched. Nested blocks
(ingest, sources, anarlog, auto_changes) merge into what is already there, so
saving one keep-out list keeps the others.

Nothing here needs a daemon reload: every allowlisted key is read from config
at the point of use.
"""
from __future__ import annotations

import threading
from typing import Callable

from mcpbrain import config

_SOURCES = ("gmail", "drive", "calendar")
_INGEST_LISTS = ("generated_gmail_subject_prefixes", "generated_drive_folders",
                 "exclude_senders", "exclude_labels")
_REVIEW_MIN, _REVIEW_MAX = 1, 500

# One writer at a time: read, merge and write must not interleave with another
# settings save.
_lock = threading.Lock()


class SettingsError(ValueError):
    """A body the allowlist refuses; the message is the client-facing error."""


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_review_cap(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and _REVIEW_MIN <= v <= _REVIEW_MAX


def _mode_check(kind: str) -> Callable:
    return lambda v: isinstance(v, str) and v in config.auto_change_modes(kind)


# Nested blocks: {block: {key: validator}}. Top-level scalars: {key: validator}.
_NESTED: dict[str, dict[str, Callable]] = {
    "auto_changes": {k: _mode_check(k) for k in config.AUTO_CHANGE_KINDS},
    "sources": {k: _is_bool for k in _SOURCES},
    "anarlog": {"enabled": _is_bool},
    "ingest": {k: _is_str_list for k in _INGEST_LISTS},
}
_TOP: dict[str, Callable] = {
    "review_max_apply_per_run": _is_review_cap,
    "owner_aliases": _is_str_list,
}
_HINTS = {
    "review_max_apply_per_run": f"a whole number from {_REVIEW_MIN} to {_REVIEW_MAX}",
    "owner_aliases": "a list of strings",
    "sources": "true or false",
    "anarlog": "true or false",
    "ingest": "a list of strings",
}


def _bad_value(key: str, block: str | None = None) -> SettingsError:
    if block == "auto_changes":
        allowed = ", ".join(config.auto_change_modes(key))
        return SettingsError(f"invalid value for auto_changes.{key}: expected one of {allowed}")
    name = f"{block}.{key}" if block else key
    return SettingsError(f"invalid value for {name}: expected {_HINTS[block or key]}")


def _validate(body: dict) -> None:
    # Unknown keys first, across the whole body, so a typo is reported as such
    # even when another key's value is also wrong.
    for key, val in body.items():
        if key in _NESTED:
            if isinstance(val, dict):
                for sub in val:
                    if sub not in _NESTED[key]:
                        raise SettingsError(f"unknown key: {key}.{sub}")
        elif key not in _TOP:
            raise SettingsError(f"unknown key: {key}")
    for key, val in body.items():
        if key in _NESTED:
            if not isinstance(val, dict):
                raise SettingsError(f"invalid value for {key}: expected an object")
            for sub, v in val.items():
                if not _NESTED[key][sub](v):
                    raise _bad_value(sub, key)
        elif not _TOP[key](val):
            raise _bad_value(key)


def get_settings(home) -> dict:
    """Every allowlisted key with the value in effect now (defaults filled in)."""
    home = str(home)
    cfg = config.read_config(home)
    src = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    anarlog = cfg.get("anarlog") if isinstance(cfg.get("anarlog"), dict) else {}
    aliases = cfg.get("owner_aliases")
    return {
        "auto_changes": {k: config.auto_change_mode(home, k) for k in config.AUTO_CHANGE_KINDS},
        "review_max_apply_per_run": config.review_max_apply_per_run(home),
        "sources": {k: bool(src.get(k, True)) for k in _SOURCES},
        "anarlog": {"enabled": bool(anarlog.get("enabled", False))},
        "ingest": {k: config._ingest_list(home, k) for k in _INGEST_LISTS},
        "owner_aliases": [str(a) for a in aliases] if isinstance(aliases, list) else [],
    }


def apply_settings(home, body: dict) -> dict:
    """Validate `body` against the allowlist and merge it into config.

    Returns {"ok": True, "settings": get_settings(home)} or
    {"ok": False, "error": "..."} with config unchanged."""
    home = str(home)
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    try:
        _validate(body)
    except SettingsError as exc:
        return {"ok": False, "error": str(exc)}
    if body:
        with _lock:
            cur = config.read_config(home)
            updates = {}
            for key, val in body.items():
                if key in _NESTED:
                    prior = cur.get(key) if isinstance(cur.get(key), dict) else {}
                    updates[key] = {**prior, **val}
                else:
                    updates[key] = val
            # write_config replaces each top-level key it is given; handing it
            # the merged block is the one-level deep merge. Atomic (temp+rename).
            config.write_config(home, updates)
    return {"ok": True, "settings": get_settings(home)}
