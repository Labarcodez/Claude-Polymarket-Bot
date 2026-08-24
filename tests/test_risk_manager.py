from __future__ import annotations

import pytest

from polybot.engine.models import Action, MarketSnapshot, PortfolioState, TradeDecision
from polybot.risk.manager import RiskManager


def make_decision(**overrides):
    base = dict(
        action=Action.BUY_YES,
        fair_value_probability=0.60,
        confidence=0.80,
        suggested_size_usd=100.0,
        time_horizon_days=5,
        reasoning="test",
        risk_flags=[],
        key_uncertainties=[],
    )
    base.update(overrides)
    return TradeDecision(**base)


def make_portfolio(**overrides):
    base = dict(bankroll_usd=1000.0, open_positions=[], trades_today=0, realized_pnl_today=0.0)
    base.update(overrides)
    return PortfolioState(**base)


def test_no_trade_action_produces_no_plan(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(action=Action.NO_TRADE, suggested_size_usd=0)
    assert rm.plan_entry_order(decision, sample_market, make_portfolio()) is None


def test_hold_action_produces_no_plan(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(action=Action.HOLD, suggested_size_usd=0)
    assert rm.plan_entry_order(decision, sample_market, make_portfolio()) is None


def test_low_confidence_is_rejected(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(confidence=0.50)  # below min_confidence=0.65
    assert rm.plan_entry_order(decision, sample_market, make_portfolio()) is None


def test_low_edge_is_rejected(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    # yes_price/best_ask_yes is 0.41; fair value 0.43 -> edge 0.02 < min_edge 0.05
    decision = make_decision(fair_value_probability=0.43)
    assert rm.plan_entry_order(decision, sample_market, make_portfolio()) is None


def test_good_buy_yes_produces_a_sized_plan(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85, suggested_size_usd=200)
    plan = rm.plan_entry_order(decision, sample_market, make_portfolio())
    assert plan is not None
    assert plan.outcome == "YES"
    assert plan.side == "BUY"
    assert plan.token_id == sample_market.yes_token_id
    assert 0 < plan.size_usd <= risk_config.max_position_usd
    # Slippage buffer nudges the price up (or leaves it, if the buffer rounds
    # back down to the same tick) -- it should never come out *below* the ask.
    assert plan.limit_price >= sample_market.best_ask_yes


def test_buy_yes_uses_a_genuinely_zero_ask_rather_than_falling_back_to_yes_price(risk_config, ai_config):
    # `market.best_ask_yes or market.yes_price` would treat a real
    # best_ask_yes of 0.0 as "missing" and silently substitute yes_price
    # instead. Construct a market where they'd disagree (0.0 vs 0.05) so a
    # regression back to `or` is caught: with the real ask (0.0), price<=0
    # short-circuits to a rejection; with the buggy fallback (0.05), the
    # trade would instead get priced and possibly accepted.
    market = MarketSnapshot(
        condition_id="0xzero", question="Edge case", slug="edge-case",
        yes_token_id="tok-yes", no_token_id="tok-no",
        yes_price=0.05, no_price=0.95, best_ask_yes=0.0, best_bid_yes=0.0,
        volume_24hr=50_000, liquidity=20_000,
    )
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.50, confidence=0.90, suggested_size_usd=100)
    assert rm.plan_entry_order(decision, market, make_portfolio()) is None


def test_buy_no_uses_no_token_and_inverted_probability(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    # Model thinks YES is much less likely than market price -> BUY_NO.
    decision = make_decision(action=Action.BUY_NO, fair_value_probability=0.20, confidence=0.85, suggested_size_usd=200)
    plan = rm.plan_entry_order(decision, sample_market, make_portfolio())
    assert plan is not None
    assert plan.outcome == "NO"
    assert plan.token_id == sample_market.no_token_id


def test_buy_no_prices_off_the_yes_bid_not_the_yes_ask(risk_config, ai_config, sample_market):
    # sample_market: best_bid_yes=0.39, best_ask_yes=0.41. The real cost to
    # buy NO is 1 - best_bid_yes = 0.61 (the NO *ask*) -- NOT
    # 1 - best_ask_yes = 0.59 (which is actually the NO *bid*, i.e. what
    # buying NO would systematically underprice it to).
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(action=Action.BUY_NO, fair_value_probability=0.30, confidence=0.85, suggested_size_usd=200)
    plan = rm.plan_entry_order(decision, sample_market, make_portfolio())
    assert plan is not None
    # 0.61 with a 1% slippage buffer (risk_config.slippage_bps=100), rounded
    # to the nearest cent tick: 0.61 * 1.01 = 0.6161 -> 0.62.
    assert plan.limit_price == pytest.approx(0.62)
    assert plan.limit_price > 0.60  # sanity: must not land at the buggy ~0.59-0.60 range


def test_zero_suggested_size_rejects_the_trade_instead_of_falling_back_to_full_risk_size(risk_config, ai_config, sample_market):
    # A model that returns suggested_size_usd=0 on an actual BUY action is
    # signaling essentially no conviction. That must result in a rejected
    # (too-small) trade, not a silent fallback to the full risk-computed size.
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85, suggested_size_usd=0.0)
    assert rm.plan_entry_order(decision, sample_market, make_portfolio()) is None


def test_position_size_never_exceeds_max_position_usd(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.95, confidence=0.99, suggested_size_usd=10_000)
    plan = rm.plan_entry_order(decision, sample_market, make_portfolio(bankroll_usd=1_000_000))
    assert plan is not None
    assert plan.size_usd <= risk_config.max_position_usd


def test_per_market_exposure_cap_blocks_second_entry(risk_config, ai_config, sample_market):
    from polybot.engine.models import OpenPosition
    from datetime import datetime, timezone

    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85)
    existing = OpenPosition(
        condition_id=sample_market.condition_id,
        token_id=sample_market.yes_token_id,
        outcome="YES",
        question=sample_market.question,
        shares=100,
        avg_cost=0.40,
        opened_ts=datetime.now(timezone.utc),
    )
    portfolio = make_portfolio(open_positions=[existing])
    # Already holding this market -> no new entry, regardless of edge/confidence.
    assert rm.plan_entry_order(decision, sample_market, portfolio) is None


def test_daily_loss_limit_blocks_new_entries(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85)
    portfolio = make_portfolio(realized_pnl_today=-risk_config.max_daily_loss_usd - 1)
    assert rm.daily_limits_reached(portfolio) is not None
    assert rm.plan_entry_order(decision, sample_market, portfolio) is None


def test_daily_trade_cap_blocks_new_entries(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85)
    portfolio = make_portfolio(trades_today=risk_config.max_daily_trades)
    assert rm.plan_entry_order(decision, sample_market, portfolio) is None


def test_zero_bankroll_produces_no_plan(risk_config, ai_config, sample_market):
    rm = RiskManager(risk_config, ai_config)
    decision = make_decision(fair_value_probability=0.65, confidence=0.85)
    assert rm.plan_entry_order(decision, sample_market, make_portfolio(bankroll_usd=0)) is None


def test_evaluate_exit_take_profit():
    rm = RiskManager.__new__(RiskManager)  # config not needed for this pure check besides thresholds
    from polybot.config import RiskConfig

    rm.risk = RiskConfig(take_profit_pct=0.30, stop_loss_pct=0.30, exit_before_resolution_hours=2)
    assert rm.evaluate_exit(current_price=0.60, avg_cost=0.40, hours_to_resolution=None) is not None


def test_evaluate_exit_stop_loss():
    from polybot.config import RiskConfig

    rm = RiskManager.__new__(RiskManager)
    rm.risk = RiskConfig(take_profit_pct=0.30, stop_loss_pct=0.30, exit_before_resolution_hours=2)
    assert rm.evaluate_exit(current_price=0.25, avg_cost=0.40, hours_to_resolution=None) is not None


def test_evaluate_exit_holds_when_within_bounds():
    from polybot.config import RiskConfig

    rm = RiskManager.__new__(RiskManager)
    rm.risk = RiskConfig(take_profit_pct=0.30, stop_loss_pct=0.30, exit_before_resolution_hours=2)
    assert rm.evaluate_exit(current_price=0.42, avg_cost=0.40, hours_to_resolution=48) is None


def test_evaluate_exit_near_resolution_forces_close():
    from polybot.config import RiskConfig

    rm = RiskManager.__new__(RiskManager)
    rm.risk = RiskConfig(take_profit_pct=0.30, stop_loss_pct=0.30, exit_before_resolution_hours=2)
    assert rm.evaluate_exit(current_price=0.41, avg_cost=0.40, hours_to_resolution=1) is not None
