"""The deployment's tenant profile — who this build belongs to.

Replaces the old `org_defaults` module, which baked the tenant's own Shared Drive
folder ids into the wheel as a SILENT FALLBACK: every consumer read
`config fleet.folder_id or org_defaults.FLEET_FOLDER_ID`, so an install that never
set the value (the common case — the wizard leaves it blank) depended entirely on
the compiled-in default, and a FORK that forgot to re-point wrote its health
beacons and encrypted backup snapshots into the upstream org's Drive with nothing
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
)

# Blank means the tenant runs without that feature — it must never mean "borrow
# someone else's". A minimal fork needs ONE repo (their own source) plus a private
# home for the OAuth client: no wheel index (install from the checkout) and no
# marketplace at all, since `mcpbrain setup` registers the MCP connector — the
# actual brain — with no plugin distribution involved.
_OPTIONAL_FIELDS: tuple[str, ...] = (
    "fleet_folder_id", "escrow_folder_id", "index_url",
    "marketplace_owner", "marketplace_repo", "marketplace_name",
)

# All three or none: an owner with no repo is a typo, not a deliberate opt-out.
_MARKETPLACE_FIELDS = ("marketplace_owner", "marketplace_repo", "marketplace_name")


class TenantNotConfigured(RuntimeError):
    """Raised by require() when this build carries no tenant profile."""


@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    display_name: str
    oauth_project_id: str
    fleet_folder_id: str | None = None
    escrow_folder_id: str | None = None
    index_url: str | None = None
    marketplace_owner: str | None = None
    marketplace_repo: str | None = None
    marketplace_name: str | None = None

    @property
    def has_marketplace(self) -> bool:
        return bool(self.marketplace_owner and self.marketplace_repo
                    and self.marketplace_name)

    @property
    def marketplace_slug(self) -> str | None:
        if not self.has_marketplace:
            return None
        return f"{self.marketplace_owner}/{self.marketplace_repo}"

    @property
    def plugin_homepage(self) -> str | None:
        slug = self.marketplace_slug
        return None if slug is None else f"https://github.com/{slug}"


def load_dict(raw: dict, source: str = "<dict>") -> TenantProfile:
    """Validate an already-parsed profile mapping. `load` is this plus file IO."""
    kwargs: dict[str, str | None] = {}
    for field in REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"tenant profile {source}: {field!r} is required and must be non-empty")
        kwargs[field] = value.strip()
    for field in _OPTIONAL_FIELDS:
        value = raw.get(field)
        # "" is UNSET, not an empty value: the wizard clears a field to opt out of
        # the org fleet, and config.fleet_defaults has always read it that way.
        kwargs[field] = value.strip() if isinstance(value, str) and value.strip() else None
    filled = [f for f in _MARKETPLACE_FIELDS if kwargs.get(f)]
    if filled and len(filled) != len(_MARKETPLACE_FIELDS):
        missing = [f for f in _MARKETPLACE_FIELDS if not kwargs.get(f)]
        raise ValueError(
            f"tenant profile {source}: marketplace is partially configured — "
            f"{', '.join(missing)} missing. Set all of "
            f"{', '.join(_MARKETPLACE_FIELDS)} or leave all blank.")
    return TenantProfile(**kwargs)  # type: ignore[arg-type]


def load(path: Path) -> TenantProfile:
    """Parse and validate a profile JSON. Raises ValueError naming the bad field."""
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid tenant profile at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid tenant profile at {path}: expected a JSON object")
    return load_dict(raw, str(path))


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
    # A tenant with no marketplace ships no plugin, so there is nothing for these
    # files to agree WITH — skip rather than invent a failure.
    if prof.has_marketplace:
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
    # The install docs must name the ORGANISATION, not a marketplace-add command.
    # The plugin ships through claude.ai organization settings, so users install it
    # from the app catalogue (Customize -> Plugins -> Browse plugins) and filter by
    # the org name — that name is the thing a fork has to change, and the thing that
    # is wrong if they do not. `claude plugin marketplace add` was checked for here
    # and was the wrong path entirely: org-settings distribution requires a PRIVATE
    # marketplace repo, so no user can add it by hand without repo credentials.
    inst = repo / "plugin" / "INSTALL.md"
    if inst.is_file() and prof.display_name not in inst.read_text():
        out.append(f"{inst}: does not name {prof.display_name!r} — users filter the "
                   f"plugin catalogue by the organisation name")
    out.extend(_check_versions(repo))
    return out


def _check_versions(repo: Path) -> list[str]:
    """The five version files must agree.

    CLAUDE.md calls the plugin manifests the easiest step to forget: bumping only
    pyproject/__init__ ships a wrong marketplace version. This is cheap to check and
    it is checked here so `bin/release.py` gets it for free.
    """
    import re
    import tomllib
    found: dict[str, str] = {}
    try:
        found["pyproject.toml"] = tomllib.loads(
            (repo / "pyproject.toml").read_text())["project"]["version"]
        init = (repo / "mcpbrain" / "__init__.py").read_text()
        found["mcpbrain/__init__.py"] = re.search(
            r'__version__\s*=\s*[\'"]([^\'"]+)', init).group(1)
        found["plugin.json"] = json.loads(
            (repo / "plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]
        found["marketplace.json"] = json.loads(
            (repo / "plugin" / ".claude-plugin" / "marketplace.json").read_text()
        )["plugins"][0]["version"]
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


def _default_fetch(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _gh_repo_probe(owner: str, repo: str) -> bool | None:
    """True/False if `gh` can definitively say whether the repo exists, else None.

    A private repo 404s to an UNAUTHENTICATED fetch exactly as a nonexistent one
    does, so an anonymous GET cannot tell "correctly private" from "you typo'd the
    owner". `gh` is already this project's GitHub tool and carries the user's auth,
    so ask it first and fall back to "cannot tell" rather than guessing.
    """
    import shutil
    import subprocess
    if not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(["gh", "api", f"repos/{owner}/{repo}", "--jq", ".name"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return True
    # Distinguish "authenticated and it is really not there" from "not logged in".
    if "Could not resolve to a Repository" in r.stderr or "HTTP 404" in r.stderr:
        return False
    return None


def check_online(prof: TenantProfile, *, drive=None, fetch=None,
                 repo_probe=None) -> tuple[list[str], list[str]]:
    """Network checks: Drive folders resolve and are writable, the index serves
    mcpbrain, the marketplace repo exists.

    Returns (problems, notes). A problem fails the check; a note is reported and
    does not. `drive` is a googleapiclient Drive v3 resource, `fetch` a url->text
    callable and `repo_probe` an (owner, repo) -> bool | None callable; all three
    are injected so this is testable without a network or credentials. A blank
    optional field is SKIPPED, not failed — a tenant running without fleet or backup
    is a valid tenant.
    """
    problems: list[str] = []
    notes: list[str] = []
    fetch = fetch or _default_fetch
    repo_probe = repo_probe or _gh_repo_probe

    for field in ("fleet_folder_id", "escrow_folder_id"):
        folder_id = getattr(prof, field)
        if not folder_id:
            continue
        if drive is None:
            problems.append(f"{field}: skipped (no Drive credentials)")
            continue
        try:
            meta = drive.files().get(
                fileId=folder_id,
                fields="mimeType,driveId,capabilities/canAddChildren",
                supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 — any Drive failure is a problem to report
            problems.append(f"{field}: {folder_id!r} could not be read ({exc})")
            continue
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            problems.append(f"{field}: {folder_id!r} is not a folder "
                            f"(mimeType {meta.get('mimeType')!r})")
            continue
        if not meta.get("driveId"):
            problems.append(f"{field}: {folder_id!r} is on My Drive, not a Shared "
                            f"Drive. The drive.file scope cannot write there.")
        if not (meta.get("capabilities") or {}).get("canAddChildren"):
            problems.append(f"{field}: {folder_id!r} is not writable by this account")

    if prof.index_url:
        url = prof.index_url.rstrip("/") + "/mcpbrain/"
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"index_url: {url} could not be fetched ({exc})")
        else:
            if "mcpbrain" not in body:
                problems.append(f"index_url: {url} does not list any mcpbrain wheel")

    # The plugin repo is normally PRIVATE (mcpbrain-plugin is), and a private repo
    # 404s anonymously exactly as a nonexistent one does. Failing on that made this
    # check exit non-zero for the correct configuration — a permanently red check
    # is one people stop reading. So: only a DEFINITIVE "not there" is a problem.
    exists = repo_probe(prof.marketplace_owner, prof.marketplace_repo)
    if exists is False:
        problems.append(
            f"marketplace: {prof.plugin_homepage} does not exist — check "
            f"tenant.json's marketplace_owner/marketplace_repo")
    elif exists is None:
        try:
            fetch(prof.plugin_homepage)
        except Exception as exc:  # noqa: BLE001
            notes.append(
                f"marketplace: could not verify {prof.plugin_homepage} ({exc}). "
                f"Expected when the repo is private and `gh` is unavailable or "
                f"logged out — confirm by hand.")
    return problems, notes
