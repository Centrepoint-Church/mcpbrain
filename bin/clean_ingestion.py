#!/usr/bin/env python3
"""Attended, reversible ingestion clean-up — thin wrapper.

The clean-up lives in the package (`mcpbrain/ingestion_cleanup.py`) so every
install gets it: the daemon runs it once as a one-shot migration, and
`mcpbrain clean-ingestion` is the installed CLI. This script keeps the
checkout path working with the same flags:

    python bin/clean_ingestion.py                 # dry run: per-step counts
    python bin/clean_ingestion.py --yes           # snapshot, apply, integrity_check
    python bin/clean_ingestion.py --undo dedup_embed --yes

See `mcpbrain.ingestion_cleanup` for the steps and the shared marker.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain.ingestion_cleanup import *  # noqa: E402,F401,F403
from mcpbrain.ingestion_cleanup import main  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
