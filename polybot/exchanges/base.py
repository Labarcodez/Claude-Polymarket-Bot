"""Common interface every exchange adapter (Polymarket, Kalshi, ...) implements.

`TradingEngine` and `ExitManager` are written entirely against this
interface -- they never import a venue-specific client directly. That's
what makes "run the same bot against Polymarket or Kalshi" a config change
(`exchange: polymarket|kalshi`) instead of a fork.

Simulated (dry_run) fills are handled centrally in engine/trader.py and
engine/exit_manager.py, not here -- adapters only need to implement the
*live* paths (fetch_raw_markets, get_midpoint, balances, and real order
execution). That keeps venue-specific code focused on the one thing that's
actually different per venue: how to talk to that venue's API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..engine.models import MarketSnapshot, OpenPosition, OrderPlan


@dataclass
class ExecutionResult:
    """The outcome of a real (non-simulated) order submission."""

    success: bool
    filled_shares: float = 0.0
    fill_price: float = 0.0
    order_id: Optional[str] = None
    status: str = ""  # e.g. "filled", "partial", "rejected", "error"
    error: Optional[str] = None
    raw: Any = field(default=None, repr=False)


class ExchangeAdapter(ABC):
    """One instance = one authenticated (or read-only) session against one venue."""

    name: str  # "polymarket" | "kalshi"

    @property
    @abstractmethod
    def read_only(self) -> bool:
        """True when no trading credentials were supplied -- market data
        reads still work, but require_trading() will raise."""

    @abstractmethod
    def require_trading(self) -> None:
        """Raise a clear RuntimeError if this adapter can't place real orders."""

    # ---- market data ---------------------------------------------------

    @abstractmethod
    def fetch_raw_markets(self, pool_size: int) -> List[MarketSnapshot]:
        """Return up to `pool_size` candidate markets, freshly fetched, in
        whatever order the venue's API naturally returns them (typically by
        volume). No filtering -- that's engine/scanner.py's job, shared
        across venues."""

    @abstractmethod
    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        """Current midpoint price (0-1) for one side of one market. Used by
        ExitManager to price open positions. Returns None on failure."""

    def enrich_candidates(self, snapshots: List[MarketSnapshot]) -> None:
        """Optional best-effort refresh of any fields worth a live re-check
        (e.g. tick size) -- called by MarketScanner only on the small,
        already-filtered final candidate list, not on the whole fetch pool,
        so it's fine for this to be a few extra API calls per cycle.
        Mutates `snapshots` in place. Default: no-op."""

    # ---- account ---------------------------------------------------------

    @abstractmethod
    def get_balance_usd(self) -> Optional[float]:
        """Available collateral/cash balance, in dollars. Requires trading
        credentials."""

    # ---- order execution (live only -- dry_run never calls these) --------

    @abstractmethod
    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        """Submit a real BUY order for a new (or added-to) position."""

    @abstractmethod
    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        """Submit a real SELL order to close (all of) an existing position.
        `price_hint` is the last observed midpoint, useful as a fallback
        limit reference for venues without a pure market-order type."""
