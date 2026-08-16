"""Unit-тесты SlidingWindowRateLimiter (#228)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest

from webhook.rate_limit import SlidingWindowRateLimiter


def test_allows_up_to_limit():
    rl = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)

    assert [rl.allow("ip", now=0) for _ in range(3)] == [True, True, True]


def test_blocks_over_limit():
    rl = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        rl.allow("ip", now=0)

    assert rl.allow("ip", now=1) is False


def test_window_expiry_frees_slots():
    rl = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
    rl.allow("ip", now=0)
    rl.allow("ip", now=10)
    assert rl.allow("ip", now=20) is False  # окно ещё держит оба хита

    # спустя окно (>60s после первого) первый хит выпал → снова можно
    assert rl.allow("ip", now=61) is True


def test_keys_are_independent():
    rl = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("ip-a", now=0) is True
    assert rl.allow("ip-b", now=0) is True
    assert rl.allow("ip-a", now=0) is False


def test_reset_specific_key():
    rl = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    rl.allow("ip", now=0)
    rl.reset("ip")
    assert rl.allow("ip", now=0) is True


def test_reset_all():
    rl = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
    rl.allow("a", now=0)
    rl.allow("b", now=0)
    rl.reset()
    assert rl.allow("a", now=0) is True
    assert rl.allow("b", now=0) is True


@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_bad_max_requests(bad):
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=bad, window_seconds=60)


@pytest.mark.parametrize("bad", [0, -5])
def test_rejects_bad_window(bad):
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=1, window_seconds=bad)
