"""Proactive detection pass — GTD projects/areas detectors removed (§9E).

run() is kept as a no-op so daemon._run_periodic_passes() calls it unchanged.
"""


def run(store, *, now: str | None = None) -> dict:
    return {"project_no_next_action": 0, "area_overdue": 0}
