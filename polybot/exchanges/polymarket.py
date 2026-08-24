"""Polymarket exchange adapter: wires the Gamma API (discovery) and
py-clob-client (pricing + execution) wrappers in polybot/clob/ into the
common ExchangeAdapter interface.

The Gamma API's JSON schema is not formally versioned. `parse_market` is
written defensively (handles fields arriving as either JSON-encoded strings
or already-parsed lists/dicts) -- see tests/test_polymarket_exchange.py.
Use `polybot inspect-market <slug>` to dump a raw payload if Polymarket
changes the schema and filtering starts silently rejecting everything.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..clob.client import PolyTradingClient
from ..clob.data_api import DataApiClient
from ..clob.gamma import GammaClient
from ..config import AppConfig, unwrap_secret
from ..engine.models import MarketSnapshot, OpenPosition, OrderPlan
from .base import ExchangeAdapter, ExecutionResult, LivePosition

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
        venue="polymarket",
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


class PolymarketExchange(ExchangeAdapter):
    name = "polymarket"

    def __init__(self, config: AppConfig):
        self.gamma = GammaClient(config.polymarket.gamma_host)
        self.data_api = DataApiClient(config.polymarket.data_host)
        self.clob = PolyTradingClient(
            host=config.polymarket.clob_host,
            chain_id=config.polymarket.chain_id,
            private_key=unwrap_secret(config.private_key),
            funder_address=config.funder_address,
            signature_type=config.polymarket.signature_type,
        )
        # The address that actually *holds* positions -- the funder/proxy
        # address for email/Magic or Safe wallets, or just the signer's own
        # address for a plain EOA.
        self._funder_address = config.funder_address

    @property
    def read_only(self) -> bool:
        return self.clob.read_only

    def require_trading(self) -> None:
        self.clob.require_trading()

    # ---- market data -------------------------------------------------------

    def fetch_raw_markets(self, pool_size: int) -> List[MarketSnapshot]:
        raw_markets = self.gamma.get_markets(
            active=True, closed=False, order="volume24hr", ascending=False, limit=pool_size,
        )
        snapshots = []
        for raw in raw_markets:
            snap = parse_market(raw)
            if snap is not None:
                snapshots.append(snap)
        return snapshots

    def enrich_candidates(self, snapshots: List[MarketSnapshot]) -> None:
        for snap in snapshots:
            try:
                snap.tick_size = self.clob.get_tick_size(snap.yes_token_id)
            except Exception:
                logger.debug("Could not fetch tick size for %s, keeping default", snap.slug)

    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        try:
            return self.clob.get_midpoint(market_ref)
        except Exception:
            logger.exception("Failed to fetch Polymarket midpoint for token %s", market_ref)
            return None

    # ---- account ---------------------------------------------------------

    def get_balance_usd(self) -> Optional[float]:
        return self.clob.get_usdc_balance()

    # ---- execution ---------------------------------------------------------

    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        try:
            resp = self.clob.place_market_buy(plan.token_id, plan.size_usd, max_price=plan.limit_price)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Polymarket LIVE buy failed", plan.question)
            return ExecutionResult(success=False, status="error", error=str(e))

        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        success = bool(resp.get("success", True)) if isinstance(resp, dict) else True
        if not success:
            return ExecutionResult(success=False, status="rejected", order_id=order_id, raw=resp)

        # FOK orders fill entirely or not at all, so a successful response
        # means the full requested dollar amount filled at ~limit_price.
        filled_shares = plan.size_usd / plan.limit_price
        return ExecutionResult(
            success=True, filled_shares=filled_shares, fill_price=plan.limit_price,
            order_id=order_id, status="filled", raw=resp,
        )

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        min_price = max(price_hint * 0.95, 0.0)  # allow a little slippage on the way out
        try:
            resp = self.clob.place_market_sell(position.token_id, position.shares, min_price=min_price)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Polymarket LIVE sell failed", position.question)
            return ExecutionResult(success=False, status="error", error=str(e))

        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        success = bool(resp.get("success", True)) if isinstance(resp, dict) else True
        if not success:
            return ExecutionResult(success=False, status="rejected", order_id=order_id, raw=resp)

        return ExecutionResult(
            success=True, filled_shares=position.shares, fill_price=price_hint,
            order_id=order_id, status="filled", raw=resp,
        )

    # ---- reconciliation ---------------------------------------------------

    def get_live_positions(self) -> Optional[List[LivePosition]]:
        self.require_trading()
        address = self._funder_address or self.clob.client.get_address()
        if not address:
            logger.warning("Could not determine a wallet address to fetch live positions for")
            return None

        try:
            # sizeThreshold=0 so a small position (this bot can size well
            # under the Data API's default 1.0-share threshold) isn't
            # silently hidden from reconciliation.
            raw = self.data_api.get_positions(user=address, sizeThreshold=0)
        except Exception:
            logger.exception("Failed to fetch live Polymarket positions for %s", address)
            return None

        positions = []
        for p in raw:
            condition_id = p.get("conditionId")
            size = p.get("size")
            if condition_id is None or size is None:
                continue
            positions.append(
                LivePosition(
                    condition_id=condition_id,
                    outcome=str(p.get("outcome") or "").upper(),
                    shares=float(size),
                    question=p.get("title") or condition_id,
                )
            )
        return positions
