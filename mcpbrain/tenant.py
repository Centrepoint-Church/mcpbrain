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


# Values the fork template ships with. Any survivor means someone copied
# tenant.example.json and did not finish filling it in.
_PLACEHOLDERS = ("REPLACE", "CHANGE-ME", "your-org", "your-github-org",
                 "Your Organisation", "Your-GitHub-Org", "example.com")


def _client_path(repo: Path) -> Path:
    return Path(repo) / "mcpbrain" / "google_oauth_client.json"


def check_offline(repo: Path) -> list[str]:
    """Validate a source tree's tenant profile without touching the network.

    Returns a list of problems; empty means pass. Never raises for a bad profile —
    the caller decides whether a problem is fatal (release.py) or advisory (doctor).
    """
    repo = Path(repo)
    problems: list[str] = []

    prof_path = repo / "mcpbrain" / "tenant.json"
    if not prof_path.is_file():
        return [f"{prof_path} is missing — copy tenant.example.json and fill it in"]
    try:
        prof = load(prof_path)
    except ValueError as exc:
        return [str(exc)]

    for field in (*REQUIRED_FIELDS, *_OPTIONAL_FIELDS):
        value = getattr(prof, field) or ""
        for token in _PLACEHOLDERS:
            if token.lower() in value.lower():
                problems.append(
                    f"tenant.json: {field} still holds the template value {value!r}")

    problems.extend(_check_client(repo, prof))
    problems.extend(_check_install_surface(repo, prof))
    return problems


def _check_client(repo: Path, prof: TenantProfile) -> list[str]:
    path = _client_path(repo)
    if not path.is_file():
        return [f"{path} is missing — run `python bin/tenant.py use <tenant-dir>`"]
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON ({exc})"]
    out: list[str] = []
    if "installed" not in raw:
        kind = ", ".join(raw) or "nothing"
        return [f"{path}: not a Desktop OAuth client — found {kind!r}, expected "
                f"'installed'. A 'web' client fails at consent with a "
                f"redirect_uri_mismatch."]
    inst = raw["installed"]
    client_id = str(inst.get("client_id", ""))
    if not client_id.endswith(".apps.googleusercontent.com"):
        out.append(f"{path}: client_id {client_id!r} is not a Google client id")
    project = str(inst.get("project_id", ""))
    if project != prof.oauth_project_id:
        out.append(
            f"{path}: project_id {project!r} does not match tenant.json's "
            f"oauth_project_id {prof.oauth_project_id!r}. If this is a fork, you are "
            f"still shipping the upstream OAuth client — create your own Desktop "
            f"client (see docs/FORKING.md).")
    return out


def _check_install_surface(repo: Path, prof: TenantProfile) -> list[str]:
    """Every shipped install surface must agree with tenant.json."""
    out: list[str] = []
    mk = repo / "plugin" / ".claude-plugin" / "marketplace.json"
    if mk.is_file():
        name = json.loads(mk.read_text()).get("name")
        if name != prof.marketplace_name:
            out.append(f"{mk}: name {name!r} != tenant.json marketplace_name "
                       f"{prof.marketplace_name!r}")
    pj = repo / "plugin" / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        home = json.loads(pj.read_text()).get("homepage")
        if home != prof.plugin_homepage:
            out.append(f"{pj}: homepage {home!r} != {prof.plugin_homepage!r}")
    if prof.index_url:
        for rel in ("plugin/scripts/install.ps1", "plugin/commands/install.md"):
            p = repo / rel
            if p.is_file() and prof.index_url not in p.read_text():
                out.append(f"{p}: does not carry tenant.json's index_url "
                           f"{prof.index_url!r}")
    inst = repo / "plugin" / "INSTALL.md"
    if inst.is_file():
        text = inst.read_text()
        for cmd in (f"claude plugin marketplace add {prof.marketplace_slug}",
                    f"claude plugin install mcpbrain@{prof.marketplace_name}"):
            if cmd not in text:
                out.append(f"{inst}: missing {cmd!r}")
    out.extend(_check_versions(repo))
    return out


def _check_versions(repo: Path) -> list[str]:
    """The five version files must agree.

    CLAUDE.md calls the plugin manifests the easiest step to forget: bumping only
    pyproject/__init__ ships a wrong marketplace version. This is cheap to check and
    it is checked here so `bin/release.py` gets it for free.

    Each file is only checked if present: a real repo always ships all four, but a
    fork validating a partial tree (or this module's own tests) should not get a
    spurious "could not be read" for a file that legitimately isn't there yet.
    """
    import re
    import tomllib
    found: dict[str, str] = {}
    try:
        pp = repo / "pyproject.toml"
        if pp.is_file():
            found["pyproject.toml"] = tomllib.loads(pp.read_text())["project"]["version"]
        init_path = repo / "mcpbrain" / "__init__.py"
        if init_path.is_file():
            match = re.search(
                r'__version__\s*=\s*[\'"]([^\'"]+)', init_path.read_text())
            if match:
                found["mcpbrain/__init__.py"] = match.group(1)
        pj = repo / "plugin" / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            found["plugin.json"] = json.loads(pj.read_text())["version"]
        mk = repo / "plugin" / ".claude-plugin" / "marketplace.json"
        if mk.is_file():
            found["marketplace.json"] = json.loads(mk.read_text())["plugins"][0]["version"]
    except (OSError, KeyError, AttributeError, ValueError) as exc:
        return [f"version files: could not be read ({exc})"]
    distinct = set(found.values())
    if len(distinct) > 1:
        return ["version files disagree: "
                + ", ".join(f"{k}={v}" for k, v in sorted(found.items()))]
    return []


def cli_main(argv=None) -> int:
    """`mcpbrain tenant check` — the installed-package entry point.

    bin/tenant.py is the source-checkout entry point (it can run `use` before
    anything is installed); both call check_offline.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="mcpbrain tenant")
    ap.add_argument("cmd", choices=["check"])
    ns = ap.parse_args(argv)
    del ns
    prof = profile()
    if prof is None:
        print("tenant: NOT CONFIGURED — fleet, backup upload and auto-update are "
              "disabled. See docs/FORKING.md.")
        return 1
    print(f"tenant: {prof.tenant_id} ({prof.display_name})")
    print(f"  fleet folder : {prof.fleet_folder_id or '(disabled)'}")
    print(f"  escrow folder: {prof.escrow_folder_id or '(disabled)'}")
    print(f"  wheel index  : {prof.index_url or '(auto-update disabled)'}")
    print(f"  marketplace  : {prof.marketplace_slug}")
    return 0
