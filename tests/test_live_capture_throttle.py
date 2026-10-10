import time

from idx_scraper import cf_transport
from idx_scraper.live_capture import _BACKOFF_STEPS, _MIN_INTERVAL, LiveCapture


def test_pacing_never_below_refill_safe_floor():
    assert LiveCapture(client=None, delay_sec=0.5)._delay == _MIN_INTERVAL
    assert LiveCapture(client=None, delay_sec=5)._delay == 5


def test_backoff_escalates_and_caps_at_sixty_seconds():
    assert list(_BACKOFF_STEPS) == sorted(_BACKOFF_STEPS)
    assert _BACKOFF_STEPS[-1] == 60


def test_revive_cooldown_skips_retry_without_launching_chrome():
    t = cf_transport.BrowserTransport()
    t._ensure_loop = lambda: None
    t._loop = None  # loop not running -> early return path
    assert t._revive_if_dead() is None
    t._loop = type("L", (), {"is_running": lambda self: True})()
    t._revive_blocked_until = time.monotonic() + 60
    assert t._revive_if_dead() is None  # cooldown active, no reopen attempt
