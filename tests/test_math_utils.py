from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.utils.math_utils import clamp, days_until, kelly_fraction, round_to_tick


def test_kelly_fraction_no_edge_is_zero():
    # Market price already equals your believed probability -> no edge -> no bet.
    assert kelly_fraction(0.5, 0.5) == 0.0


def test_kelly_fraction_positive_edge():
    # You think YES is much more likely than the market does -> positive stake.
    f = kelly_fraction(price=0.40, true_probability=0.70)
    assert f > 0
    # Sanity bound: full Kelly should never suggest staking more than 100%.
    assert f <= 1.0


def test_kelly_fraction_negative_edge_clips_to_zero():
    # You think YES is less likely than market price -> Kelly for buying YES is negative -> clipped to 0.
    f = kelly_fraction(price=0.70, true_probability=0.40)
    assert f == 0.0


def test_kelly_fraction_extreme_prices_do_not_explode():
    assert kelly_fraction(0.0, 0.9) >= 0
    assert kelly_fraction(1.0, 0.9) >= 0


def test_days_until_none():
    assert days_until(None) is None


def test_days_until_future_and_past():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = now + timedelta(days=3)
    past = now - timedelta(days=2)
    assert days_until(future, now) == 3.0
    assert days_until(past, now) == -2.0


def test_round_to_tick():
    assert round_to_tick(0.4321, 0.01) == 0.43
    assert round_to_tick(0.005, 0.01) == 0.01  # clamped up to at least one tick
    assert round_to_tick(0.999, 0.01) == 0.99  # clamped below 1


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0
    assert clamp(15, 0, 10) == 10
