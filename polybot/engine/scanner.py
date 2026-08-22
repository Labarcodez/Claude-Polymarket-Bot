"""Discovers and filters candidate markets from the Gamma API, and enriches
them with live order-book data from the CLOB before handing them to Claude.

The Gamma API's JSON schema is not formally versioned. `parse_market` is
written defensively (handles fields arriving as either JSON-encoded strings
or already-parsed lists/dicts) and `passes_filters` is a pure function so
both can be unit-tested without hitting the network -- see tests/test_scanner_filters.py.
Use `polybot inspect-market <slug>` to dump a raw payload if Polymarket
changes the schema and filtering starts silently rejecting everything.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..clob.client import PolyTradingClient
from ..clob.gamma import GammaClient
from ..config import MarketScanConfig
from .models import MarketSnapshot
from ..utils.math_utils import days_until

logger = logging.getLogger(__name__)


def _maybe_json(value: Any) -> Any:
    """Gamma often returns list-typed fields (outcomes, outcomePrices,
    clobTokenIds) as JSON-encoded strings rather than actual JSON arrays."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _extract_tags(market: Dict[str, Any]) -> List[str]:
    raw = market.get("tags")
    if not raw:
        events = market.get("events") or []
        if events and isinstance(events[0], dict):
            raw = events[0].get("tags")
    tags: List[str] = []
    for t in raw or []:
        if isinstance(t, dict):
            label = t.get("label") or t.get("slug")
            if label:
                tags.append(str(label))
        elif isinstance(t, str):
            tags.append(t)
    return tags


def parse_market(market: Dict[str, Any]) -> Optional[MarketSnapshot]:
    """Convert one raw Gamma /markets entry into a MarketSnapshot, or None if
    it's missing fields we need (e.g. not CLOB-enabled, not binary)."""
    if not market.get("enableOrderBook", True):
        return None

    outcomes = _maybe_json(market.get("outcomes"))
    outcome_prices = _maybe_json(market.get("outcomePrices"))
    token_ids = _maybe_json(market.get("clobTokenIds"))

    if not (isinstance(outcomes, list) and isinstance(outcome_prices, list) and isinstance(token_ids, list)):
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None  # only trade simple binary YES/NO markets

    try:
        yes_idx = [o.strip().lower() for o in outcomes].index("yes")
    except ValueError:
        yes_idx = 0
    no_idx = 1 - yes_idx

    try:
        yes_price = float(outcome_prices[yes_idx])
        no_price = float(outcome_prices[no_idx])
    except (ValueError, TypeError, IndexError):
        return None

    end_date = None
    end_raw = market.get("endDate") or market.get("end_date_iso")
    if end_raw:
        try:
            end_date = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        except ValueError:
            end_date = None

    best_bid = market.get("bestBid")
    best_ask = market.get("bestAsk")

    return MarketSnapshot(
        condition_id=market.get("conditionId") or market.get("condition_id") or market.get("id"),
        question=market.get("question", ""),
        slug=market.get("slug", ""),
        description=market.get("description") or "",
        yes_token_id=str(token_ids[yes_idx]),
        no_token_id=str(token_ids[no_idx]),
        yes_price=yes_price,
        no_price=no_price,
        best_bid_yes=float(best_bid) if best_bid not in (None, "") else None,
        best_ask_yes=float(best_ask) if best_ask not in (None, "") else None,
        spread=float(market["spread"]) if market.get("spread") not in (None, "") else None,
        tick_size=float(market.get("orderPriceMinTickSize") or 0.01),
        neg_risk=bool(market.get("negRisk", False)),
        volume_24hr=float(market.get("volume24hr") or market.get("volume_24hr") or 0),
        liquidity=float(market.get("liquidity") or 0),
        end_date=end_date,
        tags=_extract_tags(market),
    )


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
    def __init__(self, gamma: GammaClient, cfg: MarketScanConfig, clob: Optional[PolyTradingClient] = None):
        self.gamma = gamma
        self.cfg = cfg
        self.clob = clob

    def scan(self) -> List[MarketSnapshot]:
        raw_markets = self.gamma.get_markets(
            active=True,
            closed=False,
            order="volume24hr",
            ascending=False,
            limit=self.cfg.fetch_pool_size,
        )
        logger.info("Fetched %d candidate markets from Gamma", len(raw_markets))

        candidates: List[MarketSnapshot] = []
        for raw in raw_markets:
            snap = parse_market(raw)
            if snap is None or not snap.condition_id or not snap.yes_token_id:
                continue
            if not passes_filters(snap, self.cfg):
                continue
            candidates.append(snap)
            if len(candidates) >= self.cfg.max_markets_per_cycle:
                break

        logger.info("%d markets passed filters (of %d fetched)", len(candidates), len(raw_markets))

        if self.clob is not None:
            for snap in candidates:
                self._enrich_with_orderbook(snap)

        return candidates

    def _enrich_with_orderbook(self, snap: MarketSnapshot) -> None:
        """Best-effort: refresh price/tick data from the live CLOB. Falls
        back to the Gamma-derived snapshot values on any failure so a single
        flaky read doesn't kill the whole cycle."""
        try:
            snap.tick_size = self.clob.get_tick_size(snap.yes_token_id)
        except Exception:
            logger.debug("Could not fetch tick size for %s, keeping default", snap.slug)
