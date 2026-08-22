"""Pure math helpers: Kelly sizing, price rounding, time-to-resolution."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def kelly_fraction(price: float, true_probability: float) -> float:
    """Kelly-optimal fraction of bankroll to stake buying a share priced at
    `price` (paying `price`, receiving $1 if it resolves YES for that share)
    when you believe the true win probability is `true_probability`.

    Derivation: treat this as a bet with fractional odds b = (1 - price) / price
    (profit per unit staked if you win). Standard Kelly: f* = p - (1 - p) / b,
    where p is your believed win probability. Substituting b gives the
    formula below. Returns 0 (never negative -- a negative Kelly means "bet
    the other side", which the caller handles by evaluating the other side's
    own price/probability separately, not by shorting this one).
    """
    price = min(max(price, 1e-4), 1 - 1e-4)
    true_probability = min(max(true_probability, 0.0), 1.0)

    b = (1 - price) / price
    q = 1 - true_probability
    f_star = true_probability - q / b
    return max(f_star, 0.0)


def days_until(dt: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    """Days from `now` until `dt`. Returns None if dt is None. Negative if in the past."""
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (dt - now).total_seconds() / 86400.0


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest valid tick, clamped to (0, 1)."""
    if tick_size <= 0:
        return round(price, 4)
    ticks = round(price / tick_size)
    rounded = ticks * tick_size
    decimals = max(0, len(str(tick_size).split(".")[-1])) if "." in str(tick_size) else 0
    rounded = round(rounded, decimals)
    return min(max(rounded, tick_size), 1 - tick_size)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
