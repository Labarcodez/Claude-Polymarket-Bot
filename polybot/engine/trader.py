"""Top-level orchestration: one `run_cycle()` call does everything --
reviews existing positions for exits, scans for new candidate markets, asks
Claude for an opinion on each, risk-sizes the ones worth acting on, and
executes (or, in dry_run mode, simulates) the resulting orders.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..ai.analyst import ClaudeAnalyst
from ..clob.client import PolyTradingClient
from ..clob.gamma import GammaClient
from ..config import AppConfig
from ..risk.manager import RiskManager
from ..storage.db import Database
from .exit_manager import ExitManager
from .models import OrderPlan, PortfolioState
from .scanner import MarketScanner

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.logging.db_path)

        self.gamma = GammaClient(config.polymarket.gamma_host)
        self.clob: Optional[PolyTradingClient] = None
        if config.private_key:
            self.clob = PolyTradingClient(
                host=config.polymarket.clob_host,
                chain_id=config.polymarket.chain_id,
                private_key=config.private_key,
                funder_address=config.funder_address,
                signature_type=config.polymarket.signature_type,
            )
        else:
            logger.warning(
                "No POLYMARKET_PRIVATE_KEY set -- running read-only. "
                "Position exits and entries will be simulated but cannot read live order books "
                "for exit pricing, and live trading is unavailable."
            )

        self.scanner = MarketScanner(self.gamma, config.market_scan, self.clob)
        self.analyst = ClaudeAnalyst(config.anthropic_api_key, config.ai)
        self.risk_manager = RiskManager(config.risk, config.ai)
        self.exit_manager = ExitManager(
            self.risk_manager, self.db, self.clob, dry_run=not config.is_live
        )

    @property
    def dry_run(self) -> bool:
        return not self.config.is_live

    def run_cycle(self) -> None:
        logger.info("=== cycle start (mode=%s) ===", self.config.mode)

        self.exit_manager.review_all()

        portfolio = self._load_portfolio_state()
        block_reason = self.risk_manager.daily_limits_reached(portfolio)
        if block_reason:
            logger.warning("New entries blocked this cycle: %s", block_reason)
            logger.info("=== cycle end ===")
            return

        markets = self.scanner.scan()
        for market in markets:
            try:
                decision = self.analyst.analyze(market)
            except Exception:
                logger.exception("[%s] analysis failed, skipping", market.slug)
                continue

            market_price = market.best_ask_yes or market.yes_price
            plan = self.risk_manager.plan_entry_order(decision, market, portfolio)
            self.db.record_decision(
                market.condition_id, market.slug, decision, market_price, executed=plan is not None
            )

            if plan is None:
                continue

            self._execute_entry(plan, market.tick_size, market.end_date)
            # Keep local portfolio view in sync so limits are respected within this cycle too.
            portfolio = self._load_portfolio_state()
            block_reason = self.risk_manager.daily_limits_reached(portfolio)
            if block_reason:
                logger.warning("Stopping cycle early: %s", block_reason)
                break

        logger.info("=== cycle end ===")

    def _execute_entry(self, plan: OrderPlan, tick_size: float, end_date) -> None:
        if self.dry_run or self.clob is None:
            logger.info(
                "[DRY RUN] would BUY %s %s $%.2f @ <=%.4f -- %s",
                plan.outcome, plan.question, plan.size_usd, plan.limit_price, plan.reasoning[:140],
            )
            self.db.record_order(plan, status="simulated", order_id=None, dry_run=True)
            shares = plan.size_usd / plan.limit_price
            self.db.open_position(plan, plan.limit_price, shares, end_date.isoformat() if end_date else None)
            return

        try:
            resp = self.clob.place_market_buy(plan.token_id, plan.size_usd, max_price=plan.limit_price)
        except Exception:
            logger.exception("[%s] LIVE order failed", plan.question)
            self.db.record_order(plan, status="error", order_id=None, dry_run=False)
            return

        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        success = bool(resp.get("success", True)) if isinstance(resp, dict) else True
        self.db.record_order(plan, status="submitted" if success else "rejected", order_id=order_id, dry_run=False, raw_response=resp)

        if success:
            shares = plan.size_usd / plan.limit_price
            self.db.open_position(plan, plan.limit_price, shares, end_date.isoformat() if end_date else None)
            logger.info("[LIVE] BUY %s %s $%.2f submitted (order_id=%s)", plan.outcome, plan.question, plan.size_usd, order_id)
        else:
            logger.warning("[LIVE] order for %s was not accepted: %s", plan.question, resp)

    def _load_portfolio_state(self) -> PortfolioState:
        open_positions = self.db.get_open_positions()
        stats = self.db.get_today_stats()

        bankroll = self.config.risk.bankroll_usd
        if bankroll <= 0:
            if self.clob is not None and not self.dry_run:
                live_balance = self.clob.get_usdc_balance()
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
