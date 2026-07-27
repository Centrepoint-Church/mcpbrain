"""A wall-clock budget for one pass through the daemon's bulk-work cycle.

The cycle loop used to run every phase to completion, so a large ingest or a
stalled socket held the loop for hours and everything scheduled after it —
notably the ~20 maintenance passes — never ran. Phases now take a Budget,
check it between units of work, and yield when it expires. Work is resumed on
the next tick: the bulk phases are driven by DB predicates (embedded=0,
enriched=0) and delta tokens, so they are naturally resumable and need no
explicit cursor.
"""
from __future__ import annotations

import time


class Budget:
    """Expires `deadline_s` seconds after construction. `deadline_s=None` is unbounded."""

    def __init__(self, deadline_s: float | None, clock=time.monotonic):
        self._clock = clock
        self._deadline_s = deadline_s
        self._start = clock()

    def expired(self) -> bool:
        if self._deadline_s is None:
            return False
        return (self._clock() - self._start) >= self._deadline_s

    def remaining(self) -> float:
        if self._deadline_s is None:
            return float("inf")
        return max(0.0, self._deadline_s - (self._clock() - self._start))
