"""Deterministic, non-negotiable risk management.

This layer is what stands between "Claude thinks this is a great trade" and
an actual order. It never trusts the model's own suggested size, and it can
veto a trade entirely regardless of how confident the model claims to be.
Every limit here is a hard cap, not a suggestion.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import AIConfig, RiskConfig
from ..engine.models import Action, MarketSnapshot, OrderPlan, PortfolioState, TradeDecision
from ..utils.math_utils import clamp, kelly_fraction, round_to_tick

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, risk_config: RiskConfig, ai_config: AIConfig):
        self.risk = risk_config
        self.ai = ai_config

    # ---- circuit breakers, checked once per cycle before any new entries ---

    def daily_limits_reached(self, portfolio: PortfolioState) -> Optional[str]:
        """Returns a human-readable reason if new entries should be blocked
        for the rest of the day, else None. Existing positions can still be
        exited even when this is tripped."""
        if portfolio.realized_pnl_today <= -abs(self.risk.max_daily_loss_usd):
            return (
                f"Daily loss limit hit: realized P&L today is "
                f"${portfolio.realized_pnl_today:,.2f} (limit -${self.risk.max_daily_loss_usd:,.2f})"
            )
        if portfolio.trades_today >= self.risk.max_daily_trades:
            return f"Daily trade cap hit: {portfolio.trades_today}/{self.risk.max_daily_trades} trades today"
        if len(portfolio.open_positions) >= self.risk.max_open_positions:
            return (
                f"Max open positions reached: "
                f"{len(portfolio.open_positions)}/{self.risk.max_open_positions}"
            )
        return None

    # ---- entry sizing --------------------------------------------------

    def plan_entry_order(
        self,
        decision: TradeDecision,
        market: MarketSnapshot,
        portfolio: PortfolioState,
    ) -> Optional[OrderPlan]:
        """Turn a TradeDecision into a concrete, risk-sized OrderPlan, or
        None if the trade doesn't clear the bar. Logs the reason either way."""

        if decision.action not in (Action.BUY_YES, Action.BUY_NO):
            logger.info("[%s] no entry: model action=%s", market.slug, decision.action.value)
            return None

        if decision.confidence < self.ai.min_confidence:
            logger.info(
                "[%s] rejected: confidence %.2f < min_confidence %.2f",
                market.slug, decision.confidence, self.ai.min_confidence,
            )
            return None

        if self.daily_limits_reached(portfolio):
            return None

        if portfolio.exposure_in_market(market.condition_id) > 0:
            logger.info("[%s] rejected: already have an open position in this market", market.slug)
            return None

        outcome, token_id, price, true_prob = self._resolve_side(decision, market)
        if price is None or price <= 0 or price >= 1:
            logger.info("[%s] rejected: no usable market price for %s", market.slug, outcome)
            return None

        edge = true_prob - price
        if edge < self.ai.min_edge:
            logger.info(
                "[%s] rejected: edge %.3f < min_edge %.3f (true_prob=%.3f price=%.3f)",
                market.slug, edge, self.ai.min_edge, true_prob, price,
            )
            return None

        size_usd = self._size_position(price, true_prob, market, portfolio)
        # Cap to the model's own suggested size as a sanity bound. A
        # straight min() -- not "use suggested_size_usd unless it's falsy" --
        # so a model that actually says 0 (extremely low conviction, even
        # though it's asked to reserve that for HOLD/NO_TRADE) results in
        # size 0 and gets rejected below by min_order_usd, rather than
        # silently discarding that signal and sizing off risk alone.
        size_usd = min(size_usd, max(decision.suggested_size_usd, 0.0))

        if size_usd < self.risk.min_order_usd:
            logger.info(
                "[%s] rejected: sized position $%.2f below min_order_usd $%.2f",
                market.slug, size_usd, self.risk.min_order_usd,
            )
            return None

        limit_price = self._buy_limit_price(price, market.tick_size)

        plan = OrderPlan(
            venue=market.venue,
            condition_id=market.condition_id,
            token_id=token_id,
            question=market.question,
            outcome=outcome,
            side="BUY",
            size_usd=round(size_usd, 2),
            limit_price=limit_price,
            # A hint the exchange adapter interprets in its own terms: an
            # all-or-nothing fill-or-kill order on Polymarket, an aggressive
            # crossing limit order on Kalshi (which has no FOK order type).
            order_type="FOK" if market.venue == "polymarket" else "LIMIT",
            decision_confidence=decision.confidence,
            fair_value_probability=decision.fair_value_probability,
            reasoning=decision.reasoning,
        )
        logger.info(
            "[%s] PLAN: %s %s $%.2f @ %.4f (edge=%.3f, confidence=%.2f)",
            market.slug, plan.side, plan.outcome, plan.size_usd, plan.limit_price, edge, decision.confidence,
        )
        return plan

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _resolve_side(decision: TradeDecision, market: MarketSnapshot):
        """Map a BUY_YES/BUY_NO decision to (outcome, token_id, market_price, true_probability)."""
        if decision.action == Action.BUY_YES:
            # `or` would treat a legitimate (if unusual) best_ask_yes of
            # exactly 0.0 the same as "missing", silently substituting the
            # Gamma-quoted yes_price instead of the real (zero) ask.
            price = market.best_ask_yes if market.best_ask_yes is not None else market.yes_price
            return "YES", market.yes_token_id, price, decision.fair_value_probability
        else:  # BUY_NO
            # On a complementary binary market, the cost to BUY NO is the
            # complement of the YES *bid*, not the YES ask: someone bidding
            # to buy YES at best_bid_yes is equivalent to someone asking to
            # sell NO at (1 - best_bid_yes), which is what actually has to
            # be paid to buy NO right now. (Using best_ask_yes here instead
            # gives the NO *bid* -- systematically underpricing NO by the
            # full spread, which both overstates edge and produces a limit
            # price too low to realistically fill.)
            price = (
                1 - market.best_bid_yes if market.best_bid_yes is not None else market.no_price
            )
            return "NO", market.no_token_id, price, 1 - decision.fair_value_probability

    def _size_position(
        self,
        price: float,
        true_prob: float,
        market: MarketSnapshot,
        portfolio: PortfolioState,
    ) -> float:
        bankroll = portfolio.bankroll_usd
        if bankroll <= 0:
            return 0.0

        kelly = kelly_fraction(price, true_prob) * self.risk.kelly_fraction
        raw_size = bankroll * kelly

        remaining_total = max(
            bankroll * self.risk.max_total_exposure_pct - portfolio.total_exposure_usd, 0.0
        )
        remaining_market = max(
            bankroll * self.risk.max_exposure_per_market_pct
            - portfolio.exposure_in_market(market.condition_id),
            0.0,
        )

        size = min(raw_size, self.risk.max_position_usd, remaining_total, remaining_market)
        return max(size, 0.0)

    def _buy_limit_price(self, market_price: float, tick_size: float) -> float:
        """Add a small favorable-to-fill buffer on top of the observed ask so
        a GTC order actually crosses the spread, then round to a valid tick."""
        buffered = market_price * (1 + self.risk.slippage_bps / 10_000)
        buffered = clamp(buffered, tick_size, 1 - tick_size)
        return round_to_tick(buffered, tick_size)

    # ---- exits -------------------------------------------------------------

    def evaluate_exit(
        self,
        current_price: float,
        avg_cost: float,
        hours_to_resolution: Optional[float],
    ) -> Optional[str]:
        """Rule-based exit check for an existing position. Returns a reason
        string if the position should be closed now, else None."""
        if avg_cost <= 0:
            return None
        pnl_pct = (current_price - avg_cost) / avg_cost

        if pnl_pct >= self.risk.take_profit_pct:
            return f"take_profit ({pnl_pct:+.1%} vs cost)"
        if pnl_pct <= -self.risk.stop_loss_pct:
            return f"stop_loss ({pnl_pct:+.1%} vs cost)"
        if hours_to_resolution is not None and hours_to_resolution <= self.risk.exit_before_resolution_hours:
            return f"approaching_resolution ({hours_to_resolution:.1f}h left)"
        return None
