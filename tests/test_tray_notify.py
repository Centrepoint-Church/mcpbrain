from mcpbrain.tray import _next_notification


def test_notify_on_new_attention():
    msg, na, nr = _next_notification(["Access expired — reconnect"], "", 0, 0)
    assert msg == "Access expired — reconnect" and na == "Access expired — reconnect"


def test_no_repeat_notification_same_attention():
    msg, _, _ = _next_notification(["Access expired — reconnect"], "Access expired — reconnect", 0, 0)
    assert msg is None


def test_falls_back_to_review_count():
    msg, _, nr = _next_notification([], "", 3, 0)
    assert "3 items to review" in msg and nr == 3
