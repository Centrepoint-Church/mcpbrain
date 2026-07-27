"""'Queue clear' must not be reported when the producer is simply starved."""
from mcpbrain import dashboard


def test_queue_state_idle_when_nothing_to_do():
    assert dashboard.queue_state(queued=0, unenriched_eligible=0) == "idle"


def test_queue_state_working_when_units_are_queued():
    assert dashboard.queue_state(queued=5, unenriched_eligible=1000) == "working"


def test_queue_state_starved_when_backlog_exists_but_nothing_queued():
    """The live failure: 0 units queued while 64,340 chunks awaited enrichment."""
    assert dashboard.queue_state(queued=0, unenriched_eligible=64340) == "starved"
