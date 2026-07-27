"""A busy cycle must not starve the four gated passes indefinitely.

Live evidence from the reviewed build: 183 consecutive
"bulk lock held for more than 5.0s" warnings and not one gated pass run. The
cycle held _bulk_lock for all of run_one() and re-acquired 1s later, so with
non-FIFO locks the maintenance thread lost essentially every race.
"""
import threading
import time

from mcpbrain import daemon as d


def _dm():
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_wanted = threading.Event()
    dm._bulk_lock_wait_s = 5.0
    dm._stop = threading.Event()
    return dm


def test_cycle_yields_when_maintenance_wants_the_lock():
    dm = _dm()
    got = []

    def maintenance():
        dm._bulk_lock_wanted.set()
        try:
            acquired = dm._bulk_lock.acquire(timeout=dm._bulk_lock_wait_s)
            got.append(acquired)
            if acquired:
                dm._bulk_lock.release()
        finally:
            dm._bulk_lock_wanted.clear()

    # Cycle does 20 short "phases", entering the bulk section each time.
    def cycle():
        for _ in range(20):
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    time.sleep(0.05)
    m = threading.Thread(target=maintenance, daemon=True)
    m.start()
    m.join(timeout=10)
    c.join(timeout=10)

    assert got == [True], "maintenance never got the bulk lock while the cycle ran"


def test_bulk_section_releases_between_phases():
    """The lock must not be held across the whole cycle."""
    dm = _dm()
    seen_free = []

    def watcher():
        for _ in range(50):
            if dm._bulk_lock.acquire(blocking=False):
                dm._bulk_lock.release()
                seen_free.append(1)
                return
            time.sleep(0.01)

    def cycle():
        for _ in range(10):
            with dm._cycle_bulk_section():
                time.sleep(0.01)

    w = threading.Thread(target=watcher, daemon=True)
    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    w.start()
    w.join(timeout=5)
    c.join(timeout=5)
    assert seen_free, "bulk lock was never observably free during the cycle"
