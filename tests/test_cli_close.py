"""Regression test for `polybot close`: a genuinely-zero current price
(a near-worthless outcome) must not be treated the same as a failed price
fetch. `fetched_price or pos.avg_cost` would silently record the close at
cost basis (~$0 P&L) instead of the real near-total loss."""
from __future__ import annotations

from typing import List, Optional

from click.testing import CliRunner

import polybot.exchanges as exchanges_module
from polybot.cli import main
from polybot.engine.models import OpenPosition, OrderPlan
from polybot.exchanges.base import ExchangeAdapter, ExecutionResult
from polybot.storage.db import Database


class FixedPriceExchange(ExchangeAdapter):
    name = "polymarket"

    def __init__(self, midpoint: Optional[float]):
        self._midpoint = midpoint

    @property
    def read_only(self) -> bool:
        return False

    def require_trading(self) -> None:
        pass

    def fetch_raw_markets(self, pool_size: int):
        return []

    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        return self._midpoint

    def get_balance_usd(self) -> Optional[float]:
        return None

    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        raise NotImplementedError

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        raise NotImplementedError


def _write_config(tmp_path, db_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"exchange: polymarket\n"
        f"logging:\n"
        f"  db_path: {db_path}\n"
        f"  log_path: {tmp_path / 'polybot.log'}\n"
    )
    return config_path


def test_close_records_a_genuinely_zero_price_as_a_real_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "close.db"
    db = Database(str(db_path))
    plan = OrderPlan(
        condition_id="0xcond", token_id="tok-yes", question="Will X happen?", outcome="YES", side="BUY",
        size_usd=40, limit_price=0.40, decision_confidence=0.8, fair_value_probability=0.6, reasoning="x",
    )
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    monkeypatch.setattr(exchanges_module, "build_exchange", lambda cfg: FixedPriceExchange(midpoint=0.0))

    config_path = _write_config(tmp_path, db_path)
    result = CliRunner().invoke(main, ["--config", str(config_path), "close", "0xcond"])

    assert result.exit_code == 0, result.output
    assert "at 0.0000" in result.output  # closed at the real (zero) price, not avg_cost

    # Realized P&L must reflect the near-total loss (-0.40 * 100 shares),
    # not ~$0 (which is what falling back to avg_cost would have recorded).
    assert db.get_all_time_realized_pnl() == (0.0 - 0.40) * 100


def test_close_falls_back_to_avg_cost_only_when_the_fetch_actually_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "close2.db"
    db = Database(str(db_path))
    plan = OrderPlan(
        condition_id="0xcond", token_id="tok-yes", question="Will X happen?", outcome="YES", side="BUY",
        size_usd=40, limit_price=0.40, decision_confidence=0.8, fair_value_probability=0.6, reasoning="x",
    )
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    # midpoint=None simulates an actual fetch failure.
    monkeypatch.setattr(exchanges_module, "build_exchange", lambda cfg: FixedPriceExchange(midpoint=None))

    config_path = _write_config(tmp_path, db_path)
    result = CliRunner().invoke(main, ["--config", str(config_path), "close", "0xcond"])

    assert result.exit_code == 0, result.output
    assert "at 0.4000" in result.output  # fell back to avg_cost
    assert db.get_all_time_realized_pnl() == 0.0  # closed at cost -> no P&L
