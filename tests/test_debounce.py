from cupboard_door.sensors import BookMonitor, BookPullDetector, Debouncer

DB = 0.05  # 50 ms debounce
LOCK = 3.0


def test_debouncer_ignores_short_glitch():
    d = Debouncer(True)
    assert not d.update(False, 0.000, DB)
    assert not d.update(False, 0.030, DB)
    assert not d.update(True, 0.040, DB)  # back before 50 ms: ignored
    assert not d.update(False, 0.060, DB)
    assert not d.update(False, 0.100, DB)  # only 40 ms since this new change
    assert d.stable is True


def test_debouncer_accepts_steady_change():
    d = Debouncer(True)
    d.update(False, 0.0, DB)
    assert d.update(False, 0.05, DB)
    assert d.stable is False


def feed(det, readings, start=0.0, step=0.005):
    """Feed (pressed) readings every 5 ms. Returns the number of triggers."""
    triggers, t = 0, start
    for pressed in readings:
        triggers += det.update(pressed, t, DB, LOCK)
        t += step
    return triggers


def test_one_bouncy_pull_gives_one_trigger():
    det = BookPullDetector(initially_pressed=True)
    bouncy_release = [False, True, False, True, False] + [False] * 40
    assert feed(det, bouncy_release) == 1


def test_leaving_book_out_does_not_repeat():
    det = BookPullDetector(True)
    assert feed(det, [False] * 2000) == 1


def test_putting_book_back_does_not_trigger():
    det = BookPullDetector(False)  # book already out
    assert feed(det, [True] * 40) == 0


def test_lockout_blocks_quick_second_pull():
    det = BookPullDetector(True)
    pull_and_return = [False] * 20 + [True] * 20  # 200 ms cycle
    assert feed(det, pull_and_return * 3, start=0.0) == 1  # all within 3 s
    assert feed(det, pull_and_return, start=10.0) == 1  # well after the lockout


def test_no_trigger_at_startup_when_book_is_out():
    det = BookPullDetector(initially_pressed=False)
    assert feed(det, [False] * 100) == 0


def test_book_monitor_calls_back_once(store):
    state = {"pressed": True, "t": 0.0}
    calls = []
    mon = BookMonitor(lambda: state["pressed"], store, on_pull=lambda: calls.append(1), clock=lambda: state["t"])
    state["pressed"] = False
    for _ in range(30):
        mon.poll_once()
        state["t"] += 0.005
    assert calls == [1]
