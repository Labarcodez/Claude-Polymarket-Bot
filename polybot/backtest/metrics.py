"""Turns a backtest run's SQLite ledger into a report: P&L, drawdown,
win rate, and -- the metric that actually answers "is the strategy adding
information, not just noise" -- Brier score of the model's own probability
estimates against realized outcomes, compared to the market's own implied
probability as a baseline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ..storage.db import Database


@dataclass
class BacktestReport:
    strategy_name: str
    starting_bankroll: float
    ending_bankroll: float
    return_pct: Optional[float]
    total_decisions: int
    trades_executed: int
    trades_closed: int
    win_rate: Optional[float]
    total_realized_pnl: float
    max_drawdown_pct: Optional[float]
    # Brier score: mean squared error between a stated probability and the
    # 0/1 realized outcome, over decisions whose market later resolved.
    # Lower is better; 0 is a perfect forecaster, 0.25 is what a coin-flip
    # ("always guess 50%") scores against a 50/50 base rate. Comparing the
    # strategy's own Brier score to the market's (using the market price at
    # decision time as its own "prediction") is the real test of whether
    # the strategy is adding information beyond what the market already
    # knew, not just how profitable a lucky sample happened to be.
    brier_score: Optional[float]
    market_implied_brier_score: Optional[float]
    brier_terms_n: int
    avg_edge_at_execution: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        def fmt(v, spec="{:.4f}"):
            return spec.format(v) if v is not None else "n/a"

        lines = [
            f"Strategy: {self.strategy_name}",
            f"Decisions evaluated: {self.total_decisions}  |  Trades executed: {self.trades_executed}  |  Trades closed: {self.trades_closed}",
            f"Starting bankroll: ${self.starting_bankroll:,.2f}  ->  Ending: ${self.ending_bankroll:,.2f}  ({fmt(self.return_pct, '{:+.2f}')}%)",
            f"Total realized P&L: ${self.total_realized_pnl:+,.2f}",
            f"Win rate: {fmt(self.win_rate, '{:.1%}')}   Max drawdown: {fmt(self.max_drawdown_pct, '{:.1%}')}",
            f"Avg |edge| at execution (true_prob vs. traded-side price, as the risk manager saw it): {fmt(self.avg_edge_at_execution)}",
            f"Brier score -- strategy: {fmt(self.brier_score)}   market-implied: {fmt(self.market_implied_brier_score)}   (n={self.brier_terms_n}, lower is better)",
        ]
        return "\n".join(lines)


def compute_report(
    db: Database,
    venue: str,
    strategy_name: str,
    starting_bankroll: float,
    resolutions: Dict[str, Tuple[str, datetime]],
) -> BacktestReport:
    decisions = db.get_recent_decisions(venue=venue, limit=None)
    closed = db.get_closed_positions(venue=venue)

    total_decisions = len(decisions)
    trades_executed = sum(1 for d in decisions if d["executed"])
    trades_closed = len(closed)
    wins = sum(1 for p in closed if p["realized_pnl"] > 0)
    win_rate = wins / trades_closed if trades_closed else None
    total_realized_pnl = sum(p["realized_pnl"] for p in closed)
    ending_bankroll = starting_bankroll + total_realized_pnl
    return_pct = (total_realized_pnl / starting_bankroll * 100) if starting_bankroll else None

    equity = starting_bankroll
    peak = equity
    max_dd = 0.0
    for p in closed:
        equity += p["realized_pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    brier_terms = []
    market_brier_terms = []
    edge_terms = []
    for d in decisions:
        res = resolutions.get(d["condition_id"])
        if res is None:
            continue
        outcome, _resolved_ts = res
        actual = 1.0 if outcome == "YES" else 0.0
        model_p = d["fair_value_probability"]
        market_p = d["market_price"]
        if model_p is not None:
            brier_terms.append((model_p - actual) ** 2)
        if market_p is not None:
            market_brier_terms.append((market_p - actual) ** 2)
        if d["executed"] and d["edge_at_execution"] is not None:
            # The signed edge the risk manager actually used to accept the
            # trade (RiskManager._resolve_side, side-appropriate) -- NOT
            # abs(model_p - market_p): market_p above is always a
            # YES-ask-anchored figure and is the wrong reference price for a
            # BUY_NO trade, which is priced off (1 - best_bid_yes) instead.
            edge_terms.append(abs(d["edge_at_execution"]))

    return BacktestReport(
        strategy_name=strategy_name,
        starting_bankroll=starting_bankroll,
        ending_bankroll=ending_bankroll,
        return_pct=return_pct,
        total_decisions=total_decisions,
        trades_executed=trades_executed,
        trades_closed=trades_closed,
        win_rate=win_rate,
        total_realized_pnl=total_realized_pnl,
        max_drawdown_pct=max_dd if closed else None,
        brier_score=(sum(brier_terms) / len(brier_terms)) if brier_terms else None,
        market_implied_brier_score=(sum(market_brier_terms) / len(market_brier_terms)) if market_brier_terms else None,
        brier_terms_n=len(brier_terms),
        avg_edge_at_execution=(sum(edge_terms) / len(edge_terms)) if edge_terms else None,
    )
