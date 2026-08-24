from __future__ import annotations

import pytest

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
        suggested_size_usd=10, time_horizon_days=3, reasoning="x", risk_flags=["thin_liquidity"], key_uncertainties=[],
    )
    db.record_decision("0xcond", "slug", decision, market_price=0.5, executed=True, question="Will X happen?")
    plan = make_plan()
    db.record_order(plan, status="simulated", order_id=None, dry_run=True)

    [row] = db.get_recent_decisions()
    assert row["condition_id"] == "0xcond"
    assert row["slug"] == "slug"
    assert row["question"] == "Will X happen?"  # regression check: this used to always be recorded as NULL
    assert row["action"] == "BUY_YES"
    assert row["fair_value_probability"] == 0.6
    assert row["confidence"] == 0.8
    assert row["market_price"] == 0.5
    assert row["executed"] == 1
    assert row["venue"] == "polymarket"
    assert "thin_liquidity" in row["risk_flags"]


def test_get_recent_decisions_filters_by_venue_and_orders_newest_first(tmp_path):
    db = make_db(tmp_path)
    decision = TradeDecision(
        action=Action.NO_TRADE, fair_value_probability=0.5, confidence=0.5,
        suggested_size_usd=0, time_horizon_days=0, reasoning="x", risk_flags=[], key_uncertainties=[],
    )
    db.record_decision("0xa", "a", decision, market_price=0.5, executed=False, venue="polymarket", question="A?")
    db.record_decision("KXB", "KXB", decision, market_price=0.5, executed=False, venue="kalshi", question="B?")
    db.record_decision("0xc", "c", decision, market_price=0.5, executed=False, venue="polymarket", question="C?")

    poly = db.get_recent_decisions(venue="polymarket")
    assert [r["question"] for r in poly] == ["C?", "A?"]  # newest first

    kalshi = db.get_recent_decisions(venue="kalshi")
    assert [r["question"] for r in kalshi] == ["B?"]

    assert len(db.get_recent_decisions()) == 3


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


def test_closing_a_position_does_not_double_count_against_daily_trades(tmp_path):
    # trades_count feeds RiskManager.max_daily_trades, which -- per its own
    # docstring -- gates new *entries* only. Exits must not consume that
    # budget, or a day of legitimate stop-loss/take-profit exits would
    # spuriously block new entries.
    db = make_db(tmp_path)
    plan = make_plan()
    db.open_position(plan, fill_price=0.42, shares=20, end_date=None)
    stats = db.get_today_stats()
    assert stats["trades_count"] == 1

    db.close_position(plan.condition_id, fill_price=0.50, reason="manual_close")
    stats = db.get_today_stats()
    assert stats["trades_count"] == 1  # unchanged by the exit
    assert stats["realized_pnl"] > 0


def test_reopening_a_previously_closed_position_is_a_fresh_position(tmp_path):
    db = make_db(tmp_path)
    plan = make_plan(size_usd=40.0, limit_price=0.40)
    db.open_position(plan, fill_price=0.40, shares=100, end_date=None)
    db.close_position(plan.condition_id, fill_price=0.55, reason="take_profit")
    assert db.get_open_positions() == []

    # Re-enter the same market later -- must NOT silently merge into the
    # old (closed) row's shares/avg_cost while leaving status='closed',
    # which would hide real, live shares from get_open_positions() (and
    # therefore from ExitManager/RiskManager) forever.
    plan2 = make_plan(size_usd=20.0, limit_price=0.30)
    db.open_position(plan2, fill_price=0.30, shares=50, end_date=None)

    positions = db.get_open_positions()
    assert len(positions) == 1  # confirms the row is actually visible via get_open_positions again
    assert positions[0].shares == 50   # fresh position, not merged with the old 100
    assert positions[0].avg_cost == 0.30  # fresh cost basis, not blended with the old 0.40


def test_adding_to_an_already_open_position_still_averages_correctly(tmp_path):
    # The averaging behavior for a genuinely still-open position (e.g. two
    # fills on the same cycle) must keep working after the closed-row fix.
    db = make_db(tmp_path)
    plan_a = make_plan(size_usd=40.0, limit_price=0.40)
    plan_b = make_plan(size_usd=40.0, limit_price=0.60)

    db.open_position(plan_a, fill_price=0.40, shares=100, end_date=None)
    db.open_position(plan_b, fill_price=0.60, shares=100, end_date=None)

    positions = db.get_open_positions()
    assert len(positions) == 1
    assert positions[0].shares == 200
    assert positions[0].avg_cost == 0.50  # (100*0.40 + 100*0.60) / 200


def test_open_position_refuses_to_blend_the_opposite_outcome_into_an_open_row(tmp_path):
    # Defense in depth: this table holds one outcome per market (PK is
    # condition_id alone). If RiskManager's own "no re-entry into a held
    # market" guard were ever bypassed, blending a NO fill into an open YES
    # row (or vice versa) must fail loudly, not silently corrupt the cost basis.
    db = make_db(tmp_path)
    yes_plan = make_plan(outcome="YES", token_id="tok-yes")
    db.open_position(yes_plan, fill_price=0.40, shares=100, end_date=None)

    no_plan = make_plan(outcome="NO", token_id="tok-no")
    with pytest.raises(ValueError, match="already has an open YES position"):
        db.open_position(no_plan, fill_price=0.60, shares=50, end_date=None)

    # The original YES position must be untouched by the rejected call.
    [pos] = db.get_open_positions()
    assert pos.outcome == "YES"
    assert pos.shares == 100
    assert pos.avg_cost == 0.40
