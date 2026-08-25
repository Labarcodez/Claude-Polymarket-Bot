"""Pluggable analysis strategies for the backtest engine. Everything
downstream of `decide()` -- filtering, sizing, exits -- is the same
production code the live bot uses; only this one step differs from a real
run.
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod

from ..engine.models import Action, MarketSnapshot, TradeDecision


class BacktestStrategy(ABC):
    name: str

    @abstractmethod
    def decide(self, market: MarketSnapshot) -> TradeDecision: ...


class NullStrategy(BacktestStrategy):
    """Always NO_TRADE. A sanity baseline -- a backtest run against this
    should show zero executed trades and exactly $0 P&L. If it doesn't,
    something in the engine is broken."""

    name = "null"

    def decide(self, market: MarketSnapshot) -> TradeDecision:
        return TradeDecision(
            action=Action.NO_TRADE, fair_value_probability=market.yes_price, confidence=0.0,
            suggested_size_usd=0, time_horizon_days=0, reasoning="null strategy: never trades",
            risk_flags=[], key_uncertainties=[],
        )


class RandomStrategy(BacktestStrategy):
    """Seeded random decisions carrying no real information -- a naive
    noise trader. Expected to lose money after spread/slippage against an
    efficient market. This exists to confirm the engine correctly charges
    real execution costs (it should show a mildly negative return on the
    synthetic calibrated demo data, from slippage alone, not to search for
    real edge -- there isn't any here by construction)."""

    name = "random"

    def __init__(self, seed: int = 7, trade_probability: float = 0.35):
        self._rng = random.Random(seed)
        self.trade_probability = trade_probability

    def decide(self, market: MarketSnapshot) -> TradeDecision:
        if self._rng.random() > self.trade_probability:
            return TradeDecision(
                action=Action.NO_TRADE, fair_value_probability=market.yes_price, confidence=0.0,
                suggested_size_usd=0, time_horizon_days=1, reasoning="random strategy: passed",
                risk_flags=[], key_uncertainties=[],
            )
        action = self._rng.choice([Action.BUY_YES, Action.BUY_NO])
        noise = self._rng.uniform(0.05, 0.15) * self._rng.choice([-1, 1])
        fair_value = min(max(market.yes_price + noise, 0.01), 0.99)
        return TradeDecision(
            action=action, fair_value_probability=fair_value,
            confidence=self._rng.uniform(0.65, 0.90), suggested_size_usd=self._rng.uniform(10, 50),
            time_horizon_days=self._rng.uniform(1, 10),
            reasoning="random strategy: no real information used, a synthetic noise-trader baseline",
            risk_flags=["synthetic_noise_trader"], key_uncertainties=["this decision used no real information"],
        )


class ClaudeStrategy(BacktestStrategy):
    """Wraps the real ClaudeAnalyst. Costs a real API call per market
    evaluated -- use on a modest sample, not a full multi-year replay.

    IMPORTANT (see docs/BACKTESTING.md): a resolved historical market is a
    look-ahead risk for an LLM strategy specifically, in a way it isn't for
    a rule-based one. If the market concerns an event the model may have
    seen discussed in its training data (a past election, a well-known
    sports result), its "prediction" is not really forecasting anything --
    it may be recalling the answer. Keep this in mind when this strategy's
    backtest results look surprisingly good."""

    name = "claude"

    def __init__(self, analyst):
        self.analyst = analyst

    def decide(self, market: MarketSnapshot) -> TradeDecision:
        return self.analyst.analyze(market)
