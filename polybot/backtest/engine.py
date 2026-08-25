"""Replays historical market snapshots through the real scanner-filter and
RiskManager code, one timestamp at a time, exactly mirroring one
TradingEngine.run_cycle() per tick -- see the package docstring for why
that reuse matters.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from ..config import AppConfig
from ..engine.models import Action, MarketSnapshot, PortfolioState
from ..engine.scanner import passes_filters
from ..risk.manager import RiskManager
from ..storage.db import Database
from ..utils.math_utils import days_until
from .strategies import BacktestStrategy

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, config: AppConfig, strategy: BacktestStrategy, db_path: str):
        self.config = config
        self.strategy = strategy
        self.db = Database(db_path)
        self.risk_manager = RiskManager(config.risk, config.ai)
        self.venue = config.exchange
        self.starting_bankroll = config.risk.bankroll_usd or 1000.0

    def run(
        self,
        snapshots: List[Tuple[datetime, List[MarketSnapshot]]],
        resolutions: Dict[str, Tuple[str, datetime]],
    ) -> None:
        resolved: Set[str] = set()
        last_ts: Optional[datetime] = None
        last_markets: List[MarketSnapshot] = []

        for ts, markets in snapshots:
            last_ts, last_markets = ts, markets
            price_by_cond = {m.condition_id: m for m in markets}

            self._settle_resolutions(ts, resolutions, resolved)
            self._review_exits(ts, price_by_cond, resolved)

            portfolio = self._portfolio_state(ts)
            if self.risk_manager.daily_limits_reached(portfolio):
                continue

            candidates = [
                m for m in markets
                if m.condition_id not in resolved and passes_filters(m, self.config.market_scan, now=ts)
            ]
            candidates.sort(key=lambda m: -m.volume_24hr)
            candidates = candidates[: self.config.market_scan.max_markets_per_cycle]

            for market in candidates:
                try:
                    decision = self.strategy.decide(market)
                except Exception:
                    logger.exception("[%s] strategy failed at %s, skipping", market.slug, ts)
                    continue

                market_price = market.best_ask_yes if market.best_ask_yes is not None else market.yes_price
                # Edge as the risk manager actually computed it for the
                # traded side (RiskManager._resolve_side / plan_entry_order:
                # true_prob - price), NOT abs(model_p - market_price) --
                # market_price above is always YES-ask-anchored and is wrong
                # for BUY_NO, where the risk manager priced off
                # (1 - best_bid_yes) instead. Reusing the real staticmethod
                # here (rather than re-deriving the formula) keeps this from
                # drifting from production the same way the rest of this
                # engine does.
                edge_at_execution = None
                if decision.action in (Action.BUY_YES, Action.BUY_NO):
                    _outcome, _token_id, side_price, true_prob = RiskManager._resolve_side(decision, market)
                    if side_price is not None:
                        edge_at_execution = true_prob - side_price

                plan = self.risk_manager.plan_entry_order(decision, market, portfolio)
                self.db.record_decision(
                    market.condition_id, market.slug, decision, market_price,
                    executed=plan is not None, venue=self.venue, question=market.question, as_of=ts,
                    edge_at_execution=edge_at_execution,
                )
                if plan is None:
                    continue

                shares = plan.size_usd / plan.limit_price
                end_date_str = market.end_date.isoformat() if market.end_date else None
                self.db.open_position(plan, plan.limit_price, shares, end_date_str, as_of=ts)

                portfolio = self._portfolio_state(ts)
                if self.risk_manager.daily_limits_reached(portfolio):
                    break

        self._final_mark_to_market(last_ts, last_markets)

    # ---- internals -------------------------------------------------------

    def _settle_resolutions(
        self, ts: datetime, resolutions: Dict[str, Tuple[str, datetime]], resolved: Set[str]
    ) -> None:
        for pos in self.db.get_open_positions(venue=self.venue):
            if pos.condition_id in resolved:
                continue
            res = resolutions.get(pos.condition_id)
            if res is None:
                continue
            outcome, resolved_ts = res
            if resolved_ts <= ts:
                settle_price = 1.0 if outcome == pos.outcome.upper() else 0.0
                self.db.close_position(pos.condition_id, settle_price, reason="resolved", as_of=ts)
                resolved.add(pos.condition_id)

    def _review_exits(
        self, ts: datetime, price_by_cond: Dict[str, MarketSnapshot], resolved: Set[str]
    ) -> None:
        for pos in self.db.get_open_positions(venue=self.venue):
            if pos.condition_id in resolved:
                continue
            snap = price_by_cond.get(pos.condition_id)
            if snap is None:
                continue
            current_price = snap.yes_price if pos.outcome.upper() == "YES" else snap.no_price
            days_left = days_until(pos.end_date, ts)
            hours_left = days_left * 24 if days_left is not None else None
            reason = self.risk_manager.evaluate_exit(current_price, pos.avg_cost, hours_left)
            if reason:
                self.db.close_position(pos.condition_id, current_price, reason, as_of=ts)

    def _portfolio_state(self, ts: datetime) -> PortfolioState:
        open_positions = self.db.get_open_positions(venue=self.venue)
        stats = self.db.get_today_stats(venue=self.venue, as_of=ts)
        # Deliberately NOT compounding with cumulative realized P&L here.
        # The backtest CLI always sets a fixed config.risk.bankroll_usd
        # (defaulting to $1000, never 0 unless a user explicitly passes
        # --starting-bankroll 0), and resolve_bankroll()/TradingEngine in
        # live trading return that exact fixed value every cycle in that
        # configuration -- they only read a live, cumulative balance when
        # NO fixed bankroll is configured *and* the exchange is live
        # (see polybot/engine/trader.py::resolve_bankroll). A backtest
        # can never faithfully replicate that live-balance-read branch (there
        # is no real account to read), so the only way to keep sizing
        # decisions here identical to what the same config would produce
        # live is to hold the bankroll fixed too. Compounding would silently
        # size trades differently than production does for this exact
        # configuration, defeating the point of reusing RiskManager at all.
        bankroll = self.starting_bankroll
        return PortfolioState(
            bankroll_usd=max(bankroll, 0.0),
            open_positions=open_positions,
            trades_today=stats["trades_count"],
            realized_pnl_today=stats["realized_pnl"],
        )

    def _final_mark_to_market(self, last_ts: Optional[datetime], last_markets: List[MarketSnapshot]) -> None:
        """Anything still open when the data window ends is closed at its
        last known price -- a mark-to-market accounting close, not a real
        resolution -- so the final report reflects a complete equity curve
        rather than silently ignoring still-open positions."""
        if last_ts is None:
            return
        price_by_cond = {m.condition_id: m for m in last_markets}
        for pos in self.db.get_open_positions(venue=self.venue):
            snap = price_by_cond.get(pos.condition_id)
            price = (snap.yes_price if pos.outcome.upper() == "YES" else snap.no_price) if snap else pos.avg_cost
            self.db.close_position(pos.condition_id, price, reason="backtest_end_mark_to_market", as_of=last_ts)
