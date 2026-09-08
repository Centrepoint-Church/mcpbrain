"""The deployment's tenant profile — who this build belongs to.

Replaces the old `org_defaults` module, which baked Centrepoint's Shared Drive
folder ids into the wheel as a SILENT FALLBACK: every consumer read
`config fleet.folder_id or org_defaults.FLEET_FOLDER_ID`, so an install that never
set the value (the common case — the wizard leaves it blank) depended entirely on
the compiled-in default, and a FORK that forgot to re-point wrote its health
beacons and encrypted backup snapshots into Centrepoint's Drive with nothing
anywhere saying so.

This module keeps the same convenience for a configured build and removes the
trap: optional fields are `None` when unset, and callers disable the feature
rather than reaching for someone else's infrastructure.

`tenant.json` holds NO secrets. The Drive folder ids are not secret (a folder id
only grants access to someone the Shared Drive already shares with), and the index
URL and marketplace name are public by construction. The one genuinely private
value — the OAuth client secret — lives in `google_oauth_client.json`, which is
gitignored and stamped into the wheel at build time by `bin/tenant.py use`.

Dependency rule: stdlib only. `config` imports this, so this must never import
`config`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Non-empty in every valid profile. The rest (fleet_folder_id, escrow_folder_id,
# index_url) are optional: blank means the tenant runs without that feature.
REQUIRED_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "display_name",
    "oauth_project_id",
    "marketplace_owner",
    "marketplace_repo",
    "marketplace_name",
)

_OPTIONAL_FIELDS: tuple[str, ...] = ("fleet_folder_id", "escrow_folder_id", "index_url")


class TenantNotConfigured(RuntimeError):
    """Raised by require() when this build carries no tenant profile."""


@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    display_name: str
    oauth_project_id: str
    marketplace_owner: str
    marketplace_repo: str
    marketplace_name: str
    fleet_folder_id: str | None = None
    escrow_folder_id: str | None = None
    index_url: str | None = None

    @property
    def marketplace_slug(self) -> str:
        return f"{self.marketplace_owner}/{self.marketplace_repo}"

    @property
    def plugin_homepage(self) -> str:
        return f"https://github.com/{self.marketplace_slug}"


def load(path: Path) -> TenantProfile:
    """Parse and validate a profile JSON. Raises ValueError naming the bad field."""
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid tenant profile at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid tenant profile at {path}: expected a JSON object")
    kwargs: dict[str, str | None] = {}
    for field in REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"tenant profile {path}: {field!r} is required and must be non-empty"
            )
        kwargs[field] = value.strip()
    for field in _OPTIONAL_FIELDS:
        value = raw.get(field)
        # "" is UNSET, not an empty value: the wizard clears a field to opt out of
        # the org fleet, and config.fleet_defaults has always read it that way.
        kwargs[field] = value.strip() if isinstance(value, str) and value.strip() else None
    return TenantProfile(**kwargs)  # type: ignore[arg-type]


def _bundled_path() -> Path:
    """The profile shipped beside this module inside the wheel."""
    return Path(__file__).resolve().parent / "tenant.json"


_cache: TenantProfile | None = None
_cached = False


def _clear_cache() -> None:
    """Test hook — profile() memoises, and tests move the file underneath it."""
    global _cache, _cached
    _cache, _cached = None, False


def profile() -> TenantProfile | None:
    """This build's tenant profile, or None if it carries none.

    Resolution, mirroring auth.embedded_client_config:
      1. $MCPBRAIN_TENANT — a profile path (dev, tests, a fork validating early).
         Set-but-missing warns and falls through, so a typo cannot silently read
         as "no tenant".
      2. the bundled mcpbrain/tenant.json.
      3. None.
    """
    global _cache, _cached
    if _cached:
        return _cache
    found: TenantProfile | None = None
    env = os.getenv("MCPBRAIN_TENANT")
    if env:
        p = Path(env)
        if p.exists():
            found = load(p)
        else:
            log.warning("MCPBRAIN_TENANT set to %s but not found; falling back", p)
    if found is None:
        bundled = _bundled_path()
        if bundled.exists():
            found = load(bundled)
    _cache, _cached = found, True
    return found


def require() -> TenantProfile:
    """The profile, or raise. For callers that cannot proceed without one."""
    got = profile()
    if got is None:
        raise TenantNotConfigured(
            "This build carries no tenant profile (mcpbrain/tenant.json). "
            "See docs/FORKING.md."
        )
    return got
