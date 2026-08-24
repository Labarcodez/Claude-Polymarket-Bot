"""Tests for the venue-agnostic filtering logic in engine/scanner.py.
Venue-specific parsing (Gamma JSON -> MarketSnapshot, Kalshi JSON ->
MarketSnapshot) is tested separately in test_polymarket_exchange.py and
test_kalshi_exchange.py -- passes_filters only ever sees an already-parsed
MarketSnapshot, so these tests build one directly and never touch a venue's
wire format."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.engine.models import MarketSnapshot
from polybot.engine.scanner import passes_filters


def make_snapshot(venue="polymarket", **overrides):
    base = dict(
        venue=venue,
        condition_id="ref-1",
        question="Will X happen?",
        slug="will-x-happen",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        yes_price=0.35,
        no_price=0.65,
        spread=0.02,
        volume_24hr=10_000,
        liquidity=5_000,
        end_date=datetime.now(timezone.utc) + timedelta(days=10),
        tags=["Politics"],
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def test_passes_filters_accepts_good_market(scan_config):
    assert passes_filters(make_snapshot(), scan_config) is True


def test_passes_filters_works_for_kalshi_snapshots_too(scan_config):
    # passes_filters is venue-agnostic -- a Kalshi-flavored snapshot (ticker
    # as condition_id, contract-count volume) is filtered identically.
    snap = make_snapshot(venue="kalshi", condition_id="KXTICKER-24", slug="KXTICKER-24")
    assert passes_filters(snap, scan_config) is True


def test_passes_filters_rejects_low_volume(scan_config):
    assert passes_filters(make_snapshot(volume_24hr=10), scan_config) is False


def test_passes_filters_rejects_low_liquidity(scan_config):
    assert passes_filters(make_snapshot(liquidity=10), scan_config) is False


def test_passes_filters_rejects_wide_spread(scan_config):
    assert passes_filters(make_snapshot(spread=0.50), scan_config) is False


def test_passes_filters_rejects_near_certain_price(scan_config):
    assert passes_filters(make_snapshot(yes_price=0.99, no_price=0.01), scan_config) is False


def test_passes_filters_rejects_too_soon_to_resolve(scan_config):
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    assert passes_filters(make_snapshot(end_date=soon), scan_config) is False


def test_passes_filters_rejects_too_far_to_resolve(scan_config):
    far = datetime.now(timezone.utc) + timedelta(days=365)
    assert passes_filters(make_snapshot(end_date=far), scan_config) is False


def test_passes_filters_respects_include_tags(scan_config):
    scan_config.include_tags = ["Sports"]
    assert passes_filters(make_snapshot(tags=["Politics"]), scan_config) is False


def test_passes_filters_respects_exclude_tags(scan_config):
    scan_config.exclude_tags = ["Politics"]
    assert passes_filters(make_snapshot(tags=["Politics"]), scan_config) is False
