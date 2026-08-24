"""Venue-agnostic market filtering.

Fetching and parsing raw markets is each exchange adapter's job (see
polybot/exchanges/) since the wire format is completely different per venue.
Once a venue hands back a list of `MarketSnapshot`s, though, deciding which
of them are actually worth Claude's attention is the same question
regardless of venue -- that's `passes_filters`, kept here as a pure,
independently-testable function (see tests/test_scanner_filters.py).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from ..config import MarketScanConfig
from ..exchanges.base import ExchangeAdapter
from ..utils.math_utils import days_until
from .models import MarketSnapshot

logger = logging.getLogger(__name__)


def passes_filters(snapshot: MarketSnapshot, cfg: MarketScanConfig, now: Optional[datetime] = None) -> bool:
    """Pure filtering logic against an already-parsed MarketSnapshot."""
    if snapshot.volume_24hr < cfg.min_volume_24hr:
        return False
    if snapshot.liquidity < cfg.min_liquidity:
        return False
    if not (cfg.min_price <= snapshot.yes_price <= cfg.max_price):
        return False
    if snapshot.spread is not None and snapshot.spread > cfg.max_spread:
        return False

    days = days_until(snapshot.end_date, now)
    if days is not None:
        if days < cfg.min_days_to_resolution or days > cfg.max_days_to_resolution:
            return False

    if cfg.include_tags and not (set(t.lower() for t in snapshot.tags) & set(t.lower() for t in cfg.include_tags)):
        return False
    if cfg.exclude_tags and (set(t.lower() for t in snapshot.tags) & set(t.lower() for t in cfg.exclude_tags)):
        return False

    return True


class MarketScanner:
    def __init__(self, exchange: ExchangeAdapter, cfg: MarketScanConfig):
        self.exchange = exchange
        self.cfg = cfg

    def scan(self) -> List[MarketSnapshot]:
        raw_markets = self.exchange.fetch_raw_markets(self.cfg.fetch_pool_size)
        logger.info("Fetched %d candidate markets from %s", len(raw_markets), self.exchange.name)

        candidates: List[MarketSnapshot] = []
        for snap in raw_markets:
            if not snap.condition_id or not snap.yes_token_id:
                continue
            if not passes_filters(snap, self.cfg):
                continue
            candidates.append(snap)
            if len(candidates) >= self.cfg.max_markets_per_cycle:
                break

        logger.info("%d markets passed filters (of %d fetched)", len(candidates), len(raw_markets))

        self.exchange.enrich_candidates(candidates)
        return candidates
