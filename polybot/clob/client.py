"""Wrapper around Polymarket's official `py-clob-client` for market data reads
and order execution.

Design notes:
  - `py_clob_client` is imported lazily (inside __init__) so that read-only
    workflows (e.g. `polybot scan`) don't require it to even be importable
    in environments where it isn't needed.
  - With no private key, the wrapper still works for all read endpoints
    (order book, midpoint, price, spread) -- these don't require auth on
    Polymarket's CLOB. Anything that touches your wallet (balances, orders,
    cancels) raises a clear error via `require_trading()` instead of
    silently doing nothing.
  - L2 credential derivation (`create_or_derive_api_creds`) is a network
    round-trip to Polymarket's API, and is deferred until the first call
    that actually needs it (inside `require_trading()`), rather than done
    eagerly in `__init__`. A wallet key can be configured (e.g. in
    preparation for going live later) without every read-only command --
    `polybot scan`, `polybot status` in dry_run -- taking a hard dependency
    on Polymarket's auth endpoint being reachable right now.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"


class PolyTradingClient:
    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: Optional[str],
        funder_address: Optional[str],
        signature_type: int = 0,
    ) -> None:
        from py_clob_client.client import ClobClient  # lazy import

        self.read_only = private_key is None
        self._host = host
        self._authenticated = False

        if self.read_only:
            self.client = ClobClient(host)
            logger.info("PolyTradingClient started in READ-ONLY mode (no private key set).")
        else:
            self.client = ClobClient(
                host,
                key=private_key,
                chain_id=chain_id,
                signature_type=signature_type,
                funder=funder_address or None,
            )
            logger.info(
                "PolyTradingClient configured for trading (signature_type=%s); "
                "will authenticate lazily on first trading operation.", signature_type,
            )

    # ---- read-only market data (no auth required) -------------------------

    def get_midpoint(self, token_id: str) -> float:
        r = self.client.get_midpoint(token_id)
        return float(r["mid"]) if isinstance(r, dict) else float(r)

    def get_price(self, token_id: str, side: str) -> float:
        r = self.client.get_price(token_id, side=side)
        return float(r["price"]) if isinstance(r, dict) else float(r)

    def get_spread(self, token_id: str) -> float:
        r = self.client.get_spread(token_id)
        return float(r["spread"]) if isinstance(r, dict) else float(r)

    def get_order_book(self, token_id: str) -> Any:
        return self.client.get_order_book(token_id)

    def get_tick_size(self, token_id: str) -> float:
        try:
            return float(self.client.get_tick_size(token_id))
        except Exception:
            logger.warning("Falling back to default tick size 0.01 for token %s", token_id)
            return 0.01

    # ---- trading (requires a private key) ----------------------------------

    def require_trading(self) -> None:
        if self.read_only:
            raise RuntimeError(
                "This operation requires a funded wallet. Set POLYMARKET_PRIVATE_KEY "
                "(and POLYMARKET_FUNDER_ADDRESS if using a proxy/Safe wallet) in your .env."
            )
        if not self._authenticated:
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            self._authenticated = True
            logger.info("PolyTradingClient authenticated for trading.")

    def get_usdc_balance(self) -> Optional[float]:
        """Returns available USDC.e collateral balance, in dollars."""
        self.require_trading()
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.client.get_balance_allowance(params)
            raw = resp.get("balance") if isinstance(resp, dict) else None
            if raw is None:
                return None
            return float(raw) / 1_000_000  # USDC has 6 decimals
        except Exception:
            logger.exception("Failed to fetch USDC balance")
            return None

    def place_limit_order(self, token_id: str, price: float, size: float, side: str, order_type: str = "GTC") -> Dict[str, Any]:
        """`size` is in shares, `price` is per-share (0-1)."""
        self.require_trading()
        from py_clob_client.clob_types import OrderArgs, OrderType

        args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
        signed = self.client.create_order(args)
        return self.client.post_order(signed, getattr(OrderType, order_type))

    def place_market_buy(
        self, token_id: str, amount_usd: float, max_price: Optional[float] = None, order_type: str = "FOK"
    ) -> Dict[str, Any]:
        """Buy `amount_usd` worth of shares. `max_price`, if given, is
        slippage protection (the worst per-share price this order will
        accept) -- it does not target a specific fill price. FOK (the
        default) fills entirely now or not at all, which is what lets the
        caller safely book a position immediately on a non-error response."""
        self.require_trading()
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        ot = getattr(OrderType, order_type)
        kwargs: Dict[str, Any] = dict(token_id=token_id, amount=amount_usd, side=BUY, order_type=ot)
        if max_price is not None:
            kwargs["price"] = max_price
        args = MarketOrderArgs(**kwargs)
        signed = self.client.create_market_order(args)
        return self.client.post_order(signed, ot)

    def place_market_sell(
        self, token_id: str, size_shares: float, min_price: Optional[float] = None, order_type: str = "FOK"
    ) -> Dict[str, Any]:
        """Sell `size_shares` shares. `min_price`, if given, is slippage
        protection (the worst per-share price this order will accept)."""
        self.require_trading()
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        ot = getattr(OrderType, order_type)
        kwargs: Dict[str, Any] = dict(token_id=token_id, amount=size_shares, side=SELL, order_type=ot)
        if min_price is not None:
            kwargs["price"] = min_price
        args = MarketOrderArgs(**kwargs)
        signed = self.client.create_market_order(args)
        return self.client.post_order(signed, ot)

    def cancel(self, order_id: str) -> Any:
        self.require_trading()
        return self.client.cancel(order_id)

    def cancel_all(self) -> Any:
        self.require_trading()
        return self.client.cancel_all()

    def get_open_orders(self) -> Any:
        self.require_trading()
        from py_clob_client.clob_types import OpenOrderParams

        return self.client.get_orders(OpenOrderParams())
