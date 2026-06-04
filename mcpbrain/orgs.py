"""Config-driven org taxonomy (replaces the hardcoded four-org identity).

The enrichment pipeline classifies every thread, entity, and sender against a
set of organisations: the contract's org enum, the domain->org map fed to the
extractor, the free-text alias canonicalisation, and the org-tag filter on
relation endpoints. Historically all of those were module-level literals naming
the original install's four orgs. This module makes the taxonomy a value:

  - `OrgTaxonomy` carries the configured orgs (names, email domains, aliases)
    and derives every set the pipeline consumes.
  - `DEFAULT_TAXONOMY` is the historical hardcoded taxonomy, byte-for-byte, so
    an unconfigured install behaves exactly as before.
  - `taxonomy_from_config(home)` builds one from the `orgs` key in config.json:

        "orgs": [
          {"name": "Company 1", "domains": ["company1.com"],
           "aliases": ["Company One Pty Ltd"]},
          {"name": "Personal", "domains": []}
        ]

`external` and `unknown` are reserved classification tags, always present and
never configurable as org names.

Dependency rule: this module imports only config (and stdlib), so graph_write,
enrich, contract, prepare, and lint_graph can all import it without cycles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from mcpbrain import config

log = logging.getLogger(__name__)

# Reserved classification tags: part of the org enum, never real orgs.
RESERVED_TAGS = ("external", "unknown")

# The historical hardcoded taxonomy (graph_write._DOMAIN_ORG / ORG_ALIASES as
# they stood before the config migration). DEFAULT_TAXONOMY is built from these.
_DEFAULT_DOMAIN_ORG = {
    "centrepoint.church": "Centrepoint",
    "centrepoint.com.au": "Centrepoint",
    # ACC family — national, denominational, state office
    "acc.org.au": "ACC",
    "acci.org.au": "ACC",
    "accwa.org.au": "ACC",
    "acc.net.au": "ACC",
    "acc.church": "ACC",
    "courageouschurch.org.au": "Courageous Church",
    "courageouschurch.com.au": "Courageous Church",
    "courageouschurchperth.com": "Courageous Church",
    "curtin.edu.au": "Curtin",
}

_DEFAULT_ALIASES = {
    "centrepoint church": "Centrepoint",
    "centrepoint church incorporated": "Centrepoint",
    "centrepoint.church": "Centrepoint",
    "centrepoint baptist church": "Centrepoint",
    "centrepoint": "Centrepoint",
    "courageous church": "Courageous Church",
    "courageous church perth": "Courageous Church",
    "courageous": "Courageous Church",
    "australian christian churches": "ACC",
    "acc": "ACC",
    "curtin": "Curtin",
}


@dataclass(frozen=True)
class OrgTaxonomy:
    """The set of organisations an install classifies against.

    names:      display-case canonical org names, in configured order
    domain_map: email domain -> canonical org name
    aliases:    lowercased free-text variant -> canonical org name
    """
    names: tuple[str, ...]
    domain_map: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def valid_orgs(self) -> frozenset[str]:
        """The contract org enum: configured names plus the reserved tags."""
        return frozenset(self.names) | frozenset(RESERVED_TAGS)

    @property
    def org_tags(self) -> frozenset[str]:
        """Lowercase enum values — relation endpoints matching one exactly are
        classification tags, not real entities."""
        return frozenset(n.lower() for n in self.names) | frozenset(RESERVED_TAGS)

    @property
    def domain_lines(self) -> list[str]:
        """Human-readable 'domain -> Org' lines for the extractor context."""
        return [f"{d} -> {o}" for d, o in sorted(self.domain_map.items())]

    def canonical(self, raw: str) -> str:
        """Resolve a free-text org string to its display-case canonical form.

        Unrecognised values pass through unchanged (entity orgs are free text;
        only the thread-level enum is gated, by normalise_org in contract.py).
        """
        if not raw:
            return raw
        lowered = raw.strip().lower()
        if lowered in self.aliases:
            return self.aliases[lowered]
        for known in self.names:
            if lowered == known.lower():
                return known
        return raw

    def from_email(self, email_addr: str) -> str:
        """Map an email address to its org via the domain map.

        "" for empty input, "external" for an unrecognised domain.
        """
        if not email_addr:
            return ""
        addr = email_addr.lower().strip()
        match = re.search(r"@([\w.\-]+)", addr)
        if not match:
            return ""
        domain = match.group(1)
        for known_domain, org in self.domain_map.items():
            if domain == known_domain or domain.endswith("." + known_domain):
                return org
        return "external"


DEFAULT_TAXONOMY = OrgTaxonomy(
    names=("Centrepoint", "ACC", "Courageous Church", "Curtin"),
    domain_map=dict(_DEFAULT_DOMAIN_ORG),
    aliases=dict(_DEFAULT_ALIASES),
)


def taxonomy_from_config(home=None) -> OrgTaxonomy:
    """Build the taxonomy from config.json's `orgs` key.

    Absent or empty key -> DEFAULT_TAXONOMY (the historical four orgs), so an
    unconfigured install is unchanged. Malformed entries are skipped with a
    warning rather than crashing the pipeline. Each org's own name variants
    (lowercased name) are always usable; the optional per-org `aliases` list
    adds more. Reserved tags cannot be configured as org names.
    """
    if home is None:
        home = str(config.app_dir())
    raw = config.read_config(home).get("orgs") or []
    if not isinstance(raw, list) or not raw:
        return DEFAULT_TAXONOMY

    names: list[str] = []
    domain_map: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("orgs config: skipping non-object entry %r", entry)
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name.lower() in RESERVED_TAGS:
            log.warning("orgs config: skipping entry with reserved/empty name %r", entry)
            continue
        if name in names:
            continue
        names.append(name)
        for d in entry.get("domains") or []:
            d = str(d).strip().lower().lstrip("@")
            if d:
                domain_map[d] = name
        for a in entry.get("aliases") or []:
            a = str(a).strip().lower()
            if a:
                aliases[a] = name

    if not names:
        return DEFAULT_TAXONOMY
    return OrgTaxonomy(names=tuple(names), domain_map=domain_map, aliases=aliases)
