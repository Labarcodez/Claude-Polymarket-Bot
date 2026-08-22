from __future__ import annotations

from polybot.engine.models import Action, OrderPlan, TradeDecision
from polybot.storage.db import Database


def make_db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def make_plan(**overrides):
    base = dict(
        condition_id="0xcond",
        token_id="tok-yes",
        question="Will X happen?",
        outcome="YES",
        side="BUY",
        size_usd=10.0,
        limit_price=0.42,
        order_type="FOK",
        decision_confidence=0.8,
        fair_value_probability=0.6,
        reasoning="test",
    )
    base.update(overrides)
    return OrderPlan(**base)


def test_record_decision_and_order(tmp_path):
    db = make_db(tmp_path)
    decision = TradeDecision(
        action=Action.BUY_YES, fair_value_probability=0.6, confidence=0.8,
        suggested_size_usd=10, time_horizon_days=3, reasoning="x", risk_flags=[], key_uncertainties=[],
    )
    db.record_decision("0xcond", "slug", decision, market_price=0.5, executed=True)
    plan = make_plan()
    db.record_order(plan, status="simulated", order_id=None, dry_run=True)
    # No exception = success; nothing else externally observable from these tables via the public API.


def test_open_and_close_position_tracks_pnl(tmp_path):
    db = make_db(tmp_path)
    plan = make_plan(size_usd=40.0, limit_price=0.40)
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)

    open_positions = db.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].shares == 100
    assert open_positions[0].avg_cost == 0.40

    db.close_position("0xcond", fill_price=0.55, reason="take_profit")
    assert db.get_open_positions() == []
    assert db.get_all_time_realized_pnl() == (0.55 - 0.40) * 100


def test_reopening_same_market_averages_cost(tmp_path):
    db = make_db(tmp_path)
    plan_a = make_plan(size_usd=40.0, limit_price=0.40)
    plan_b = make_plan(size_usd=40.0, limit_price=0.60)

    db.open_position(plan_a, fill_price=0.40, shares=100, end_date=None)
    db.open_position(plan_b, fill_price=0.60, shares=100, end_date=None)

    positions = db.get_open_positions()
    assert len(positions) == 1
    assert positions[0].shares == 200
    assert positions[0].avg_cost == 0.50  # (100*0.40 + 100*0.60) / 200


def test_daily_stats_increment_on_trades(tmp_path):
    db = make_db(tmp_path)
    plan = make_plan()
    db.open_position(plan, fill_price=0.42, shares=20, end_date=None)
    stats = db.get_today_stats()
    assert stats["trades_count"] == 1

    db.close_position(plan.condition_id, fill_price=0.50, reason="manual_close")
    stats = db.get_today_stats()
    assert stats["trades_count"] == 2
    assert stats["realized_pnl"] > 0
