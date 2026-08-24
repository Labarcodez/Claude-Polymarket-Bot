"""Kalshi exchange adapter: a small hand-rolled REST client (Kalshi's API key
+ RSA-PSS request signing has no dependency-free official Python SDK) wired
into the common ExchangeAdapter interface.

API reference used to build this. Kalshi's own docs site (docs.kalshi.com)
was not reachable from the environment this was written in; the request/
response field names below were instead cross-checked against Kalshi's own
swagger-generated Python SDK's model docs (github.com/lowgrind/kalshi-python,
`docs/*.md`) and corroborated across multiple independent write-ups. Still
verify against https://docs.kalshi.com if anything here looks off, and see
`polybot inspect-market` / `polybot reconcile` for ways to sanity-check
against your live account before trusting this with money:
  - Base path: {host}/trade-api/v2
  - Auth: KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE
    headers; signature = base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path)),
    salt length = digest length, signed with your uploaded RSA key pair's
    private half.
  - GET /markets, GET /markets/{ticker}, GET /markets/{ticker}/orderbook
  - GET /portfolio/balance -> {"balance": <cents>}
  - GET /portfolio/positions -> {"market_positions": [{ticker, position, ...}]}
    where `position` is signed (positive = net YES contracts, negative = net NO).
  - POST /portfolio/orders body: ticker, client_order_id, action (buy/sell),
    side (yes/no), count, type (market/limit), yes_price/no_price (cents).
    Response: {"order": {..., taker_fill_count, maker_fill_count, status}}.
  - All prices are integer cents (1-99); all sizes are whole contracts
    (unlike Polymarket, Kalshi does not support fractional share sizes).
"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from ..config import AppConfig
from ..engine.models import MarketSnapshot, OpenPosition, OrderPlan
from ..utils.math_utils import clamp
from .base import ExchangeAdapter, ExecutionResult, LivePosition

logger = logging.getLogger(__name__)

API_PREFIX = "/trade-api/v2"


def _to_cents(price: float) -> int:
    return int(clamp(round(price * 100), 1, 99))


def parse_market(market: Dict[str, Any]) -> Optional[MarketSnapshot]:
    """Convert one raw Kalshi /markets entry into a MarketSnapshot."""
    ticker = market.get("ticker")
    if not ticker:
        return None

    def cents_to_price(c: Any) -> Optional[float]:
        if c is None:
            return None
        try:
            return clamp(float(c) / 100.0, 0.0, 1.0)
        except (TypeError, ValueError):
            return None

    yes_bid = cents_to_price(market.get("yes_bid"))
    yes_ask = cents_to_price(market.get("yes_ask"))
    no_bid = cents_to_price(market.get("no_bid"))
    last_price = cents_to_price(market.get("last_price"))

    yes_price = yes_ask if yes_ask is not None else (last_price if last_price is not None else yes_bid)
    if yes_price is None:
        return None
    no_price = 1 - yes_price

    end_date = None
    close_raw = market.get("close_time") or market.get("expiration_time")
    if close_raw:
        try:
            end_date = datetime.fromisoformat(str(close_raw).replace("Z", "+00:00"))
        except ValueError:
            end_date = None

    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = max(0.0, yes_ask - yes_bid)

    tags = [str(market["category"])] if market.get("category") else []

    return MarketSnapshot(
        venue="kalshi",
        condition_id=ticker,
        question=market.get("title") or market.get("subtitle") or ticker,
        slug=ticker,
        description=market.get("subtitle") or market.get("rules_primary") or "",
        # Kalshi addresses orders by ticker + side, not distinct per-outcome
        # ids -- both fields just carry the ticker.
        yes_token_id=ticker,
        no_token_id=ticker,
        yes_price=yes_price,
        no_price=no_price,
        best_bid_yes=yes_bid,
        best_ask_yes=yes_ask,
        spread=spread,
        tick_size=0.01,  # Kalshi prices are always whole cents
        neg_risk=False,
        # NOTE: Kalshi reports volume/open_interest in *contracts* (each
        # capped at $1), not dollars. Used here as a dollar-volume/liquidity
        # proxy -- conservative in one direction (real dollar volume is at
        # most the contract count) but not an exact match to Polymarket's
        # dollar-denominated fields. Retune market_scan.min_volume_24hr /
        # min_liquidity in config when running against Kalshi.
        volume_24hr=float(market.get("volume_24h") or 0),
        liquidity=float(market.get("open_interest") or market.get("liquidity") or 0),
        end_date=end_date,
        tags=tags,
    )


class KalshiHttpClient:
    """Thin signed-REST wrapper. `api_key_id`/`private_key_pem` may be None
    for read-only usage -- unauthenticated GETs to public endpoints still work."""

    def __init__(self, host: str, api_key_id: Optional[str], private_key_pem: Optional[str], timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.api_key_id = api_key_id
        self.timeout = timeout
        self.session = requests.Session()

        self._private_key = None
        if private_key_pem:
            from cryptography.hazmat.primitives import serialization

            self._private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )

    @property
    def can_sign(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    def _sign(self, message: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        if not self.can_sign:
            return {}
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign(f"{timestamp_ms}{method.upper()}{path}")
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        headers = self._auth_headers("GET", path)
        resp = self.session.get(f"{self.host}{path}", params=clean, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        headers = {**self._auth_headers("POST", path), "Content-Type": "application/json"}
        resp = self.session.post(f"{self.host}{path}", json=body, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> Any:
        headers = self._auth_headers("DELETE", path)
        resp = self.session.delete(f"{self.host}{path}", headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else None


class KalshiExchange(ExchangeAdapter):
    name = "kalshi"

    def __init__(self, config: AppConfig):
        host = config.kalshi.demo_host if config.kalshi.use_demo else config.kalshi.api_host
        self.http = KalshiHttpClient(host, config.kalshi_api_key_id, config.kalshi_private_key_pem)
        self.status_filter = config.kalshi.status_filter
        if not self.http.can_sign:
            logger.warning(
                "No Kalshi API credentials set -- running read-only. "
                "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY) to trade."
            )

    @property
    def read_only(self) -> bool:
        return not self.http.can_sign

    def require_trading(self) -> None:
        if self.read_only:
            raise RuntimeError(
                "This operation requires Kalshi trading credentials. Set KALSHI_API_KEY_ID "
                "and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY) in your .env."
            )

    # ---- market data -------------------------------------------------------

    def fetch_raw_markets(self, pool_size: int) -> List[MarketSnapshot]:
        data = self.http.get(
            f"{API_PREFIX}/markets", status=self.status_filter, limit=min(max(pool_size, 1), 1000)
        )
        raw_markets = data.get("markets", []) if isinstance(data, dict) else []
        snapshots = []
        for raw in raw_markets:
            snap = parse_market(raw)
            if snap is not None:
                snapshots.append(snap)
        return snapshots

    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        try:
            data = self.http.get(f"{API_PREFIX}/markets/{market_ref}")
        except Exception:
            logger.exception("Failed to fetch Kalshi market %s", market_ref)
            return None
        market = data.get("market", data) if isinstance(data, dict) else {}
        if outcome.upper() == "YES":
            bid, ask = market.get("yes_bid"), market.get("yes_ask")
        else:
            bid, ask = market.get("no_bid"), market.get("no_ask")
        if bid is None or ask is None:
            return None
        return clamp(((float(bid) + float(ask)) / 2) / 100.0, 0.0, 1.0)

    # ---- account ---------------------------------------------------------

    def get_balance_usd(self) -> Optional[float]:
        self.require_trading()
        try:
            data = self.http.get(f"{API_PREFIX}/portfolio/balance")
        except Exception:
            logger.exception("Failed to fetch Kalshi balance")
            return None
        cents = data.get("balance") if isinstance(data, dict) else None
        return float(cents) / 100.0 if cents is not None else None

    # ---- execution ---------------------------------------------------------

    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        self.require_trading()
        side = "yes" if plan.outcome.upper() == "YES" else "no"
        count = max(1, round(plan.size_usd / plan.limit_price))
        body = {
            "ticker": plan.token_id,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": _to_cents(plan.limit_price),
        }
        try:
            resp = self.http.post(f"{API_PREFIX}/portfolio/orders", body)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Kalshi LIVE buy failed", plan.question)
            return ExecutionResult(success=False, status="error", error=str(e))
        return self._reconcile_fill(resp, count, plan.limit_price)

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        self.require_trading()
        side = "yes" if position.outcome.upper() == "YES" else "no"
        count = max(1, round(position.shares))
        # A floor price a bit below the last observed midpoint, so a limit
        # sell is likely to cross the book and fill now rather than rest.
        floor_price = clamp(price_hint * 0.95, 0.01, 0.99)
        body = {
            "ticker": position.token_id,
            "client_order_id": str(uuid.uuid4()),
            "action": "sell",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": _to_cents(floor_price),
        }
        try:
            resp = self.http.post(f"{API_PREFIX}/portfolio/orders", body)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Kalshi LIVE sell failed", position.question)
            return ExecutionResult(success=False, status="error", error=str(e))
        return self._reconcile_fill(resp, count, floor_price)

    @staticmethod
    def _reconcile_fill(resp: Any, requested_count: int, requested_price: float) -> ExecutionResult:
        """Figure out how many contracts actually filled from the
        order-creation response: `POST /portfolio/orders` returns
        `{"order": Order}`, and `Order.taker_fill_count` /
        `Order.maker_fill_count` (both contract-unit integers) are the fill
        counts -- confirmed against Kalshi's own swagger-generated Python
        SDK model docs (see the module docstring). A resting-then-later-filled
        order could in principle carry both a taker and a maker fill, so we
        sum them; for the aggressive crossing limit orders this adapter
        submits, the fill is almost always 100% taker.

        If a future API response doesn't carry either field (e.g. a schema
        change this project hasn't caught up with), fall back to assuming
        the full requested count filled -- so the position isn't silently
        dropped from the local ledger -- and log loudly so you can check
        your actual Kalshi account and correct the ledger with
        `polybot reconcile` / `polybot close` if needed. See
        docs/RISK_DISCLAIMER.md."""
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = order.get("order_id") or order.get("id")
        status = str(order.get("status", "")).lower()

        taker = order.get("taker_fill_count")
        maker = order.get("maker_fill_count")
        if taker is not None or maker is not None:
            filled = (taker or 0) + (maker or 0)
        else:
            logger.warning(
                "Kalshi order %s: response had neither taker_fill_count nor maker_fill_count "
                "(keys seen: %s) -- assuming the full requested count (%d) filled. "
                "Verify against your Kalshi account, e.g. with `polybot reconcile`.",
                order_id, list(order.keys()), requested_count,
            )
            filled = requested_count

        try:
            filled = int(filled)
        except (TypeError, ValueError):
            filled = requested_count

        if filled <= 0:
            return ExecutionResult(success=False, status=status or "no_fill", order_id=order_id, raw=resp)

        return ExecutionResult(
            success=True, filled_shares=float(filled), fill_price=requested_price,
            order_id=order_id, status=status or "filled", raw=resp,
        )

    # ---- reconciliation ---------------------------------------------------

    def get_live_positions(self) -> Optional[List[LivePosition]]:
        self.require_trading()
        try:
            data = self.http.get(f"{API_PREFIX}/portfolio/positions")
        except Exception:
            logger.exception("Failed to fetch live Kalshi positions")
            return None

        positions = []
        # GetPositionsResponse.market_positions[]; MarketPosition.position is
        # signed contract count: positive = net YES, negative = net NO.
        for mp in (data.get("market_positions") or []) if isinstance(data, dict) else []:
            ticker = mp.get("ticker")
            net = mp.get("position")
            if ticker is None or not net:
                continue
            positions.append(
                LivePosition(
                    condition_id=ticker,
                    outcome="YES" if net > 0 else "NO",
                    shares=float(abs(net)),
                    question=ticker,
                )
            )
        return positions
