"""Regression test for `polybot reconcile`: it must key positions by
(condition_id, outcome), not condition_id alone, or it silently drops a
live-only "other leg" position and/or reports a same-size wrong-side
position as a false OK. Found by code review; see git history for the bug
this replaced."""
from __future__ import annotations

from typing import List, Optional

from click.testing import CliRunner

import polybot.exchanges as exchanges_module
from polybot.cli import main
from polybot.engine.models import OpenPosition, OrderPlan
from polybot.exchanges.base import ExchangeAdapter, ExecutionResult, LivePosition
from polybot.storage.db import Database


class DualLegExchange(ExchangeAdapter):
    """A live account holding both YES and NO legs of the same market --
    something the local ledger can never do on its own (RiskManager refuses
    a second entry into a market already held), but which can happen from a
    manual trade, a historical position, or a partial hedge."""

    name = "polymarket"

    def __init__(self, live_positions: List[LivePosition]):
        self._live = live_positions

    @property
    def read_only(self) -> bool:
        return False

    def require_trading(self) -> None:
        pass

    def fetch_raw_markets(self, pool_size: int):
        return []

    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        return None

    def get_balance_usd(self) -> Optional[float]:
        return None

    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        raise NotImplementedError

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        raise NotImplementedError

    def get_live_positions(self) -> Optional[List[LivePosition]]:
        return self._live


def _write_config(tmp_path, db_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"exchange: polymarket\n"
        f"logging:\n"
        f"  db_path: {db_path}\n"
        f"  log_path: {tmp_path / 'polybot.log'}\n"
    )
    return config_path


def test_reconcile_flags_an_unaccounted_live_leg(tmp_path, monkeypatch):
    db_path = tmp_path / "reconcile.db"
    db = Database(str(db_path))
    plan = OrderPlan(
        condition_id="0xcond", token_id="tok-yes", question="Will X happen?", outcome="YES", side="BUY",
        size_usd=40, limit_price=0.40, decision_confidence=0.8, fair_value_probability=0.6, reasoning="x",
    )
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    # Live account matches the local YES leg exactly, but *also* holds a NO
    # leg the bot never opened and knows nothing about.
    live = [
        LivePosition(condition_id="0xcond", outcome="YES", shares=100, question="Will X happen?"),
        LivePosition(condition_id="0xcond", outcome="NO", shares=50, question="Will X happen?"),
    ]
    monkeypatch.setattr(exchanges_module, "build_exchange", lambda cfg: DualLegExchange(live))

    config_path = _write_config(tmp_path, db_path)
    result = CliRunner().invoke(main, ["--config", str(config_path), "reconcile"])

    assert result.exit_code == 0, result.output
    assert "MISMATCH" in result.output  # the unexplained NO leg must be flagged
    assert "1 mismatch" in result.output
    assert "YES" in result.output and "NO" in result.output  # both legs shown as separate rows


def test_reconcile_flags_a_same_size_wrong_side_position(tmp_path, monkeypatch):
    db_path = tmp_path / "reconcile.db"
    db = Database(str(db_path))
    plan = OrderPlan(
        condition_id="0xcond", token_id="tok-yes", question="Will X happen?", outcome="YES", side="BUY",
        size_usd=40, limit_price=0.40, decision_confidence=0.8, fair_value_probability=0.6, reasoning="x",
    )
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    # Live account actually holds 100 shares of NO, not YES -- same size,
    # wrong side (e.g. a manual trade flipped it). A condition_id-only diff
    # would see local=100/live=100 and wrongly print OK.
    live = [LivePosition(condition_id="0xcond", outcome="NO", shares=100, question="Will X happen?")]
    monkeypatch.setattr(exchanges_module, "build_exchange", lambda cfg: DualLegExchange(live))

    config_path = _write_config(tmp_path, db_path)
    result = CliRunner().invoke(main, ["--config", str(config_path), "reconcile"])

    assert result.exit_code == 0, result.output
    assert "2 mismatch" in result.output  # YES row (local 100, live 0) and NO row (local 0, live 100)


def test_reconcile_reports_ok_when_everything_matches(tmp_path, monkeypatch):
    db_path = tmp_path / "reconcile.db"
    db = Database(str(db_path))
    plan = OrderPlan(
        condition_id="0xcond", token_id="tok-yes", question="Will X happen?", outcome="YES", side="BUY",
        size_usd=40, limit_price=0.40, decision_confidence=0.8, fair_value_probability=0.6, reasoning="x",
    )
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    live = [LivePosition(condition_id="0xcond", outcome="YES", shares=100, question="Will X happen?")]
    monkeypatch.setattr(exchanges_module, "build_exchange", lambda cfg: DualLegExchange(live))

    config_path = _write_config(tmp_path, db_path)
    result = CliRunner().invoke(main, ["--config", str(config_path), "reconcile"])

    assert result.exit_code == 0, result.output
    assert "MISMATCH" not in result.output
    assert "matches your live account" in result.output
