"""Reviews every open position each cycle and closes ones that hit a
rule-based exit condition (take profit, stop loss, or approaching
resolution). Exits are pure risk-management, deliberately not routed back
through Claude -- there's no reason to pay for a fresh opinion just to
enforce a stop loss.

Written against the venue-agnostic ExchangeAdapter interface, so the same
logic closes a Polymarket or a Kalshi position without caring which.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..exchanges.base import ExchangeAdapter, ExecutionResult
from ..risk.manager import RiskManager
from ..storage.db import Database
from ..utils.math_utils import days_until
from .models import OpenPosition

logger = logging.getLogger(__name__)


class ExitManager:
    def __init__(self, risk_manager: RiskManager, db: Database, exchange: ExchangeAdapter, dry_run: bool):
        self.risk_manager = risk_manager
        self.db = db
        self.exchange = exchange
        self.dry_run = dry_run

    def review_all(self) -> None:
        positions = self.db.get_open_positions(venue=self.exchange.name)
        if not positions:
            return
        logger.info("Reviewing %d open %s position(s) for exits", len(positions), self.exchange.name)
        for pos in positions:
            self._review_one(pos)

    def _review_one(self, pos: OpenPosition) -> None:
        current_price = self.get_current_price(pos)
        if current_price is None:
            logger.warning("[%s] could not fetch current price, skipping exit check", pos.token_id)
            return

        hours_left = None
        if pos.end_date is not None:
            d = days_until(pos.end_date)
            hours_left = d * 24 if d is not None else None

        reason = self.risk_manager.evaluate_exit(current_price, pos.avg_cost, hours_left)
        if reason is None:
            return

        logger.info(
            "[%s] EXIT %s: %s shares @ cost %.4f, current %.4f -> %s",
            pos.question or pos.condition_id, pos.outcome, pos.shares, pos.avg_cost, current_price, reason,
        )
        self.close_position(pos, current_price, reason)

    def get_current_price(self, pos: OpenPosition) -> Optional[float]:
        try:
            return self.exchange.get_midpoint(pos.token_id, pos.outcome)
        except Exception:
            logger.exception("Failed to fetch midpoint for %s", pos.token_id)
            return None

    def close_position(self, pos: OpenPosition, current_price: float, reason: str) -> None:
        if self.dry_run:
            result = ExecutionResult(success=True, filled_shares=pos.shares, fill_price=current_price, status="simulated")
        else:
            try:
                result = self.exchange.execute_exit(pos, current_price)
            except Exception:
                logger.exception(
                    "[%s] live SELL order failed -- position left open in DB, will retry next cycle",
                    pos.condition_id,
                )
                return

        if not result.success:
            logger.warning("[%s] exit order did not fill: %s", pos.condition_id, result.error or result.status)
            return

        self.db.close_position(pos.condition_id, result.fill_price, reason)
