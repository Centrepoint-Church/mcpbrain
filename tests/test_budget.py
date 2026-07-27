from mcpbrain.budget import Budget


def test_not_expired_before_deadline():
    now = [100.0]
    b = Budget(deadline_s=10.0, clock=lambda: now[0])
    assert not b.expired()
    assert b.remaining() == 10.0


def test_expired_after_deadline():
    now = [100.0]
    b = Budget(deadline_s=10.0, clock=lambda: now[0])
    now[0] = 111.0
    assert b.expired()
    assert b.remaining() == 0.0


def test_zero_budget_is_immediately_expired():
    now = [5.0]
    b = Budget(deadline_s=0.0, clock=lambda: now[0])
    assert b.expired()


def test_none_budget_never_expires():
    """A None deadline means unbounded — used by tests and one-shot CLI paths."""
    b = Budget(deadline_s=None, clock=lambda: 0.0)
    assert not b.expired()
    assert b.remaining() == float("inf")
