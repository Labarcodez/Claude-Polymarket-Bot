"""Kalshi exchange adapter: a small hand-rolled REST client (Kalshi's API key
+ RSA-PSS request signing has no dependency-free official Python SDK) wired
into the common ExchangeAdapter interface.

API reference used to build this. Kalshi's docs site (docs.kalshi.com) has
never been directly fetchable from the environment this was written in, so
everything below is triangulated from multiple independent secondary
sources (search-indexed snippets of docs.kalshi.com itself, several
third-party integrations, and -- critically -- a wrapper library's GitHub
issue history that explicitly quotes "the raw spec at
docs.kalshi.com/api-reference/orders/create-order-v2.md" and documents live
verification against a real account). It has NOT been exercised against a
live Kalshi account by this project's authors. Verify against
https://docs.kalshi.com yourself before trusting it with money, and use
`polybot inspect-market` / `polybot reconcile` to sanity-check behavior
against your live account early and often.

IMPORTANT: this project originally targeted the legacy `POST
/portfolio/orders` endpoint (yes/no side, integer cents, nested under
"action"+"side"). That endpoint now returns **HTTP 410 Gone** -- confirmed
by a real integration's issue tracker hitting it live. All order mutation
now goes through the V2 endpoints below; GET endpoints (markets, balance,
positions) were unaffected by this migration and remain as before.

  - Base path: {host}/trade-api/v2
  - Auth: KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE
    headers; signature = base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path)),
    salt length = digest length, signed with your uploaded RSA key pair's
    private half.
  - GET /markets, GET /markets/{ticker}, GET /markets/{ticker}/orderbook
  - GET /portfolio/balance -> {"balance": <cents>}
  - GET /portfolio/positions -> {"market_positions": [{ticker, position, ...}]}
    where `position` is signed (positive = net YES contracts, negative = net NO).
  - POST /portfolio/events/orders (V2, current). Request fields (confirmed
    against the raw spec per the issue above): ticker, client_order_id,
    side, count, price, expiration_time, time_in_force, post_only,
    self_trade_prevention_type, cancel_order_on_pause, reduce_only,
    subaccount, order_group_id, exchange_index. Unlike the legacy endpoint:
      * `side` is "bid" (the YES direction) or "ask" (the NO direction) --
        not "yes"/"no" -- and there is no separate buy/sell `action` field;
        `reduce_only` (bool) distinguishes closing from opening instead.
      * `price` is a fixed-point DOLLAR string ("0.6200", not integer
        cents), and is always expressed as the YES-contract-equivalent
        price regardless of `side` -- an ask (NO-direction) order priced at
        "0.17" corresponds to paying $0.83 for NO, not $0.17. See
        `_yes_equivalent_price()` below for the conversion this adapter
        does so the rest of the codebase can keep thinking in "price of the
        outcome I'm actually trading" terms.
      * `count` is a string, and `time_in_force` + `self_trade_prevention_type`
        are both required (400 missing_parameters otherwise). This adapter
        uses time_in_force="fill_or_kill" for the same reason Polymarket
        orders use FOK: an all-or-nothing fill lets a non-error response
        be trusted to mean "the requested size actually filled."
    Response is a thin ack, not a full order object: {order_id,
    client_order_id?, fill_count, remaining_count, ts_ms,
    average_fill_price?} (some sources show this nested under an "order"
    key instead -- _reconcile_fill() below handles both shapes).
  - DELETE /portfolio/events/orders/{order_id} (V2 cancel).
"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from ..config import AppConfig, unwrap_secret
from ..engine.models import MarketSnapshot, OpenPosition, OrderPlan
from ..utils.math_utils import clamp
from .base import ExchangeAdapter, ExecutionResult, LivePosition

logger = logging.getLogger(__name__)

API_PREFIX = "/trade-api/v2"


def _to_cents(price: float) -> int:
    return int(clamp(round(price * 100), 1, 99))


def _to_price_string(price: float) -> str:
    """V2 order prices are fixed-point dollar strings ("0.6200"), not
    integer cents. Round to the nearest cent first (Kalshi's tick size)
    the same way _to_cents does, then format at 4 decimal places to match
    the convention seen in Kalshi's own examples and live-verified logs."""
    return f"{_to_cents(price) / 100:.4f}"


def _yes_equivalent_price(outcome: str, price_in_outcome_terms: float) -> float:
    """V2's `price` field is always expressed as the YES contract's price,
    regardless of which book side the order is on. Convert a price already
    expressed in the position's own outcome terms (price of YES if
    outcome="YES", price of NO if outcome="NO" -- how the rest of this
    codebase thinks about prices) into that YES-equivalent representation.
    Self-inverse: applying it twice returns the original value, which is
    also how it's used to convert an `average_fill_price` response back
    into outcome terms."""
    return price_in_outcome_terms if outcome.upper() == "YES" else 1 - price_in_outcome_terms


def _book_side(outcome: str) -> str:
    """Which V2 book side a transaction *in* `outcome` (buying it, or
    -- with reduce_only -- closing a position already held in it) sits on.
    "bid" is the YES direction, "ask" is the NO direction."""
    return "bid" if outcome.upper() == "YES" else "ask"


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
        self.http = KalshiHttpClient(
            host, unwrap_secret(config.kalshi_api_key_id), unwrap_secret(config.kalshi_private_key_pem)
        )
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
        count = max(1, round(plan.size_usd / plan.limit_price))
        body = {
            "ticker": plan.token_id,
            "client_order_id": str(uuid.uuid4()),
            "side": _book_side(plan.outcome),
            "count": str(count),
            "price": _to_price_string(_yes_equivalent_price(plan.outcome, plan.limit_price)),
            # FOK for the same reason Polymarket orders use FOK: an
            # all-or-nothing fill lets a non-error response be trusted to
            # mean "the requested size actually filled," with no partial-fill
            # state to reconcile.
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
        }
        try:
            resp = self.http.post(f"{API_PREFIX}/portfolio/events/orders", body)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Kalshi LIVE buy failed", plan.question)
            return ExecutionResult(success=False, status="error", error=str(e))
        return self._reconcile_fill(resp, count, plan.limit_price, plan.outcome)

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        self.require_trading()
        count = max(1, round(position.shares))
        # A floor price a bit below the last observed midpoint (in the
        # position's own outcome terms), so an aggressive FOK sell is
        # likely to cross the book and fill now rather than being killed
        # unfilled.
        floor_price = clamp(price_hint * 0.95, 0.01, 0.99)
        # Closing transacts on the OPPOSITE book side from what opened the
        # position -- a long-YES position is closed via an "ask"
        # (NO-direction) order, and vice versa -- with reduce_only so it
        # can't accidentally flip into a fresh position on the other side
        # if it somehow over-fills. Derived from _book_side() rather than
        # reimplementing the outcome->side mapping here, so the two can
        # never silently drift apart if that mapping is ever corrected.
        opening_side = _book_side(position.outcome)
        close_side = "ask" if opening_side == "bid" else "bid"
        body = {
            "ticker": position.token_id,
            "client_order_id": str(uuid.uuid4()),
            "side": close_side,
            "count": str(count),
            "price": _to_price_string(_yes_equivalent_price(position.outcome, floor_price)),
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True,
        }
        try:
            resp = self.http.post(f"{API_PREFIX}/portfolio/events/orders", body)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] Kalshi LIVE sell failed", position.question)
            return ExecutionResult(success=False, status="error", error=str(e))
        return self._reconcile_fill(resp, count, floor_price, position.outcome)

    @staticmethod
    def _reconcile_fill(resp: Any, requested_count: int, requested_price: float, outcome: str) -> ExecutionResult:
        """Figure out how many contracts actually filled from the
        order-creation response. POST /portfolio/events/orders (V2) returns
        a thin ack -- {order_id, client_order_id?, fill_count,
        remaining_count, ts_ms, average_fill_price?} -- confirmed against
        the raw V2 spec (see the module docstring); some sources show this
        nested under an "order" key instead, so both shapes are handled.

        Since every order this adapter submits is time_in_force=
        "fill_or_kill", there should be no partial-fill state: either
        fill_count equals the full requested count, or the order was
        killed entirely. If a future response doesn't carry a recognizable
        fill-count field at all (e.g. a schema change this project hasn't
        caught up with), fall back to assuming the full requested count
        filled -- so the position isn't silently dropped from the local
        ledger -- and log loudly so you can check your actual Kalshi
        account and correct the ledger with `polybot reconcile` /
        `polybot close` if needed. See docs/RISK_DISCLAIMER.md."""
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = order.get("order_id") or order.get("id")
        status = str(order.get("status", "")).lower()

        filled = order.get("fill_count")
        if filled is None:
            # Defensive fallback for the legacy (pre-410) field names, in
            # case some response variant ever carries them instead.
            taker, maker = order.get("taker_fill_count"), order.get("maker_fill_count")
            if taker is not None or maker is not None:
                filled = (taker or 0) + (maker or 0)

        if filled is None:
            logger.warning(
                "Kalshi order %s: response had no recognizable fill-count field "
                "(keys seen: %s) -- assuming the full requested count (%d) filled. "
                "Verify against your Kalshi account, e.g. with `polybot reconcile`.",
                order_id, list(order.keys()), requested_count,
            )
            filled = requested_count

        try:
            filled = int(float(filled))
        except (TypeError, ValueError):
            filled = requested_count

        if filled <= 0:
            return ExecutionResult(success=False, status=status or "no_fill", order_id=order_id, raw=resp)

        fill_price = requested_price
        avg_fill_price = order.get("average_fill_price")
        if avg_fill_price is not None:
            try:
                # average_fill_price is also YES-equivalent; convert back
                # into the position's own outcome terms for the ledger.
                fill_price = _yes_equivalent_price(outcome, float(avg_fill_price))
            except (TypeError, ValueError):
                pass

        return ExecutionResult(
            success=True, filled_shares=float(filled), fill_price=fill_price,
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
