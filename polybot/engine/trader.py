"""Top-level orchestration: one `run_cycle()` call does everything --
reviews existing positions for exits, scans for new candidate markets, asks
Claude for an opinion on each, risk-sizes the ones worth acting on, and
executes (or, in dry_run mode, simulates) the resulting orders.

This module is written entirely against the venue-agnostic `ExchangeAdapter`
interface (polybot/exchanges/base.py) -- it has no idea whether it's talking
to Polymarket or Kalshi. `config.exchange` picks the adapter at construction
time via `exchanges.build_exchange`.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..ai.analyst import ClaudeAnalyst
from ..config import AppConfig
from ..exchanges import build_exchange
from ..exchanges.base import ExchangeAdapter, ExecutionResult
from ..risk.manager import RiskManager
from ..storage.db import Database
from .exit_manager import ExitManager
from .models import OrderPlan
from .scanner import MarketScanner

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.logging.db_path)
        self.exchange: ExchangeAdapter = build_exchange(config)

        if self.exchange.read_only:
            logger.warning(
                "No trading credentials set for %s -- running read-only. "
                "Position exits/entries will be simulated but live trading is unavailable.",
                self.exchange.name,
            )

        self.scanner = MarketScanner(self.exchange, config.market_scan)
        self.analyst = ClaudeAnalyst(config.anthropic_api_key, config.ai)
        self.risk_manager = RiskManager(config.risk, config.ai)
        self.exit_manager = ExitManager(
            self.risk_manager, self.db, self.exchange, dry_run=not config.is_live
        )

    @property
    def dry_run(self) -> bool:
        return not self.config.is_live

    def run_cycle(self) -> None:
        logger.info("=== cycle start (exchange=%s, mode=%s) ===", self.config.exchange, self.config.mode)

        self.exit_manager.review_all()

        portfolio = self._load_portfolio_state()
        block_reason = self.risk_manager.daily_limits_reached(portfolio)
        if block_reason:
            logger.warning("New entries blocked this cycle: %s", block_reason)
            logger.info("=== cycle end ===")
            return

        try:
            markets = self.scanner.scan()
        except Exception:
            logger.exception("Market scan failed -- skipping the rest of this cycle")
            logger.info("=== cycle end ===")
            return

        for market in markets:
            try:
                decision = self.analyst.analyze(market)
            except Exception:
                logger.exception("[%s] analysis failed, skipping", market.slug)
                continue

            market_price = market.best_ask_yes or market.yes_price
            plan = self.risk_manager.plan_entry_order(decision, market, portfolio)
            self.db.record_decision(
                market.condition_id, market.slug, decision, market_price, executed=plan is not None,
                venue=market.venue,
            )

            if plan is None:
                continue

            self._execute_entry(plan, market.end_date)
            # Keep local portfolio view in sync so limits are respected within this cycle too.
            portfolio = self._load_portfolio_state()
            block_reason = self.risk_manager.daily_limits_reached(portfolio)
            if block_reason:
                logger.warning("Stopping cycle early: %s", block_reason)
                break

        logger.info("=== cycle end ===")

    def _execute_entry(self, plan: OrderPlan, end_date) -> None:
        if self.dry_run:
            result = ExecutionResult(
                success=True,
                filled_shares=plan.size_usd / plan.limit_price,
                fill_price=plan.limit_price,
                status="simulated",
            )
            logger.info(
                "[DRY RUN] would BUY %s %s $%.2f @ <=%.4f -- %s",
                plan.outcome, plan.question, plan.size_usd, plan.limit_price, plan.reasoning[:140],
            )
        else:
            result = self.exchange.execute_entry(plan)

        self.db.record_order(
            plan, status=result.status, order_id=result.order_id,
            dry_run=self.dry_run, raw_response=result.raw,
        )

        if not result.success or result.filled_shares <= 0:
            if not self.dry_run:
                logger.warning("[%s] LIVE entry did not fill: %s", plan.question, result.error or result.status)
            return

        self.db.open_position(
            plan, result.fill_price, result.filled_shares,
            end_date.isoformat() if end_date else None,
        )
        logger.info(
            "[%s] BUY %s %s %.2f shares @ %.4f (order_id=%s)",
            "DRY RUN" if self.dry_run else "LIVE",
            plan.outcome, plan.question, result.filled_shares, result.fill_price, result.order_id,
        )

    def _load_portfolio_state(self):
        from .models import PortfolioState

        open_positions = self.db.get_open_positions(venue=self.config.exchange)
        stats = self.db.get_today_stats(venue=self.config.exchange)

        bankroll = self.config.risk.bankroll_usd
        if bankroll <= 0:
            if not self.exchange.read_only and not self.dry_run:
                live_balance = self.exchange.get_balance_usd()
                bankroll = live_balance if live_balance is not None else 0.0
            else:
                # No configured bankroll and no way to read a live balance
                # (dry_run / no key): fall back to a nominal paper bankroll
                # so dry-run sizing logic is still exercisable end-to-end.
                bankroll = 1000.0

        return PortfolioState(
            bankroll_usd=bankroll,
            open_positions=open_positions,
            trades_today=stats["trades_count"],
            realized_pnl_today=stats["realized_pnl"],
        )
