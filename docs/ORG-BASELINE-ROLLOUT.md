# Org baseline — fleet rollout runbook

The three org flags (`org_import_enabled`, `ingest_cache_enabled`,
`org_contrib_enabled`) all default **ON**, so they are NOT the fleet-wide switch.
**The master gate is `FleetPin.is_pinned`, which is true only once a `fleet_secret`
is distributed** — until then nothing content-shaped, no cache, and no
contributions move. Enable in this order:

1. **Each install is configured** (`is_configured`: owner name + email + ≥1 org).
   Nothing enriches or bootstraps before this.
2. **Designate the curator.** On one always-on install set `role = "org_curator"`
   in `config.json` (`is_org_curator`). Only it runs the curator cadence.
3. **Curator publishes the first snapshot** (`org_curate.run` → `org-graph/manifest.json`
   in the fleet folder) BEFORE any member import is expected to succeed — a member
   importing before a snapshot exists gets `status: no_snapshot` and stays retryable.
4. **Distribute the pin.** Put an `org_pin` block (with `fleet_secret`, `embed_model`,
   `dim`, `chunker_version`) into `org-config.json` in the fleet folder. `fleet.py`'s
   allowlist (`{"cadences","org_pin"}`) permits exactly this; each install picks it up
   on next daemon start via `merge_org_config` (wholesale-replaced, so removing it
   reverts). After this, `is_pinned` is True fleet-wide.
5. **Flow begins automatically** (flags already default ON): cache publish/import,
   contributions (also gated on `is_pinned`), and snapshot import all activate. New
   users bootstrap instantly via `mcpbrain setup` → the baseline-bootstrap step.

**Disable / rollback:** remove `org_pin` from `org-config.json` → `is_pinned` reverts
on next start (cache + contributions stop; already-imported org rows remain until the
curator republishes/tombstones). Per-install opt-out: set any of the three flags false
in that install's `config.json` (an opt-out still consumes the snapshot).

**Data-safety notes:** revocation purge only fires after a drive is absent for
`ingest_cache_revocation_threshold` (default 5) consecutive cycles AND never on a
blanket-empty enumeration; contributions are typed/redacted/HMAC-referenced; the
curator re-enforces the relation allowlist as a backstop.
