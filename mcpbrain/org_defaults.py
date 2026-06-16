"""Centrepoint org defaults baked into the build.

These are the Shared Drive folder IDs the wizard pre-fills and the recovery path
falls back to when local config isn't set yet (e.g. on a fresh machine right
after Google sign-in, before the wizard has written config). They are NOT
secrets — a folder ID only grants access to someone the Shared Drive already
shares with. Keep these in sync with the pre-filled values in
mcpbrain/wizard/index.html.
"""
from __future__ import annotations

# mcpbrain-fleet/ — per-user health beacons + status.html + org-config.json.
FLEET_FOLDER_ID = "1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o"

# mcpbrain-escrow/ — per-user backup snapshots (<email>/) + escrow keys (<email>.key).
ESCROW_FOLDER_ID = "1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi"
