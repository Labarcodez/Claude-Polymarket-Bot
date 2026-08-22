"""Shared data models passed between the scanner, AI analyst, risk manager,
and executor."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Action(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class MarketSnapshot(BaseModel):
    """A point-in-time view of one tradeable (binary) market, assembled by
    the scanner from the Gamma API and (optionally) live CLOB order-book data."""

    condition_id: str
    question: str
    slug: str
    description: str = ""
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    best_bid_yes: Optional[float] = None
    best_ask_yes: Optional[float] = None
    spread: Optional[float] = None
    tick_size: float = 0.01
    neg_risk: bool = False
    volume_24hr: float = 0
    liquidity: float = 0
    end_date: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)

    @property
    def days_to_resolution(self) -> Optional[float]:
        from ..utils.math_utils import days_until

        return days_until(self.end_date)


class TradeDecision(BaseModel):
    """Claude's structured output for a single market analysis."""

    action: Action
    fair_value_probability: float = Field(..., ge=0, le=1, description="Model's estimate of P(YES)")
    confidence: float = Field(..., ge=0, le=1, description="Model's confidence in this estimate")
    suggested_size_usd: float = Field(..., ge=0)
    time_horizon_days: float = Field(..., ge=0)
    reasoning: str
    risk_flags: List[str] = Field(default_factory=list)
    key_uncertainties: List[str] = Field(default_factory=list)


class OrderPlan(BaseModel):
    """A concrete, risk-sized order the executor should place (or simulate)."""

    condition_id: str
    token_id: str
    question: str
    outcome: str  # "YES" or "NO"
    side: str  # "BUY" or "SELL"
    size_usd: float
    limit_price: float  # slippage-protection price cap, not a resting-order price
    order_type: str = "FOK"
    decision_confidence: float
    fair_value_probability: float
    reasoning: str


class OpenPosition(BaseModel):
    condition_id: str
    token_id: str
    outcome: str
    question: str
    shares: float
    avg_cost: float
    opened_ts: datetime
    end_date: Optional[datetime] = None


class PortfolioState(BaseModel):
    """Everything the risk manager needs to know about current exposure,
    assembled fresh at the top of every cycle."""

    bankroll_usd: float
    open_positions: List[OpenPosition] = Field(default_factory=list)
    trades_today: int = 0
    realized_pnl_today: float = 0.0

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.shares * p.avg_cost for p in self.open_positions)

    def exposure_in_market(self, condition_id: str) -> float:
        return sum(
            p.shares * p.avg_cost for p in self.open_positions if p.condition_id == condition_id
        )
