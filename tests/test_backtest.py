"""Tests for the backtest package: data loading/generation, and the engine
itself. Two engine tests matter most: a "no false positives" sanity check
(NullStrategy must show exactly zero trades and $0 P&L) and a "no false
negatives" positive control (a strategy handed a deliberately mispriced
market with a known-true resolution probability must actually capture that
edge) -- together they're the closest thing to proof the engine neither
fabricates edge that isn't there nor fails to act on edge that is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybot.backtest.data import generate_synthetic_dataset, load_resolutions, load_snapshots
from polybot.backtest.engine import BacktestEngine
from polybot.backtest.metrics import compute_report
from polybot.backtest.strategies import NullStrategy
from polybot.config import AppConfig
from polybot.engine.models import Action, MarketSnapshot, TradeDecision


def test_generate_and_load_synthetic_dataset_round_trips(tmp_path):
    snaps_path, res_path = generate_synthetic_dataset(
        str(tmp_path / "s.csv"), str(tmp_path / "r.csv"), num_markets=5, days=10, points_per_day=2, seed=1,
    )
    snapshots = load_snapshots(snaps_path)
    resolutions = load_resolutions(res_path)

    assert len(snapshots) > 0
    # sorted chronologically
    timestamps = [ts for ts, _markets in snapshots]
    assert timestamps == sorted(timestamps)

    all_condition_ids = {m.condition_id for _ts, markets in snapshots for m in markets}
    assert len(all_condition_ids) == 5
    assert set(resolutions.keys()) == all_condition_ids
    for outcome, resolved_ts in resolutions.values():
        assert outcome in ("YES", "NO")
        assert resolved_ts.tzinfo is not None


def test_null_strategy_backtest_produces_zero_trades_and_zero_pnl(tmp_path):
    snaps_path, res_path = generate_synthetic_dataset(
        str(tmp_path / "s.csv"), str(tmp_path / "r.csv"), num_markets=8, days=15, points_per_day=2, seed=2,
    )
    snapshots = load_snapshots(snaps_path)
    resolutions = load_resolutions(res_path)

    cfg = AppConfig()
    cfg.risk.bankroll_usd = 1000.0
    engine = BacktestEngine(cfg, NullStrategy(), db_path=str(tmp_path / "bt.db"))
    engine.run(snapshots, resolutions)

    report = compute_report(engine.db, cfg.exchange, "null", 1000.0, resolutions)
    assert report.trades_executed == 0
    assert report.trades_closed == 0
    assert report.total_realized_pnl == 0.0
    assert report.ending_bankroll == 1000.0
    # NullStrategy's fair_value_probability is always exactly market.yes_price,
    # so its Brier score is a pure measure of the *data's* calibration, and
    # must be nonzero here (the synthetic market is calibrated on average,
    # not per-market -- individual resolutions are still a coin flip weighted
    # by price, so this isn't 0).
    assert report.brier_score is not None
    assert report.total_decisions > 0


class _KnownEdgeStrategy:
    """A positive control: always states the market's *actual* future
    resolution probability (1.0 or 0.0, i.e. perfect foreknowledge) against
    a market currently priced far from it. If the engine is wired correctly
    end to end (filtering -> sizing -> fills -> settlement -> P&L), a
    strategy hard-coded to be right must show a clearly positive return."""

    name = "known_edge"

    def __init__(self, true_outcome_by_condition: dict):
        self._truth = true_outcome_by_condition

    def decide(self, market: MarketSnapshot) -> TradeDecision:
        true_outcome = self._truth[market.condition_id]
        fair_value = 0.97 if true_outcome == "YES" else 0.03
        action = Action.BUY_YES if true_outcome == "YES" else Action.BUY_NO
        return TradeDecision(
            action=action, fair_value_probability=fair_value, confidence=0.95,
            suggested_size_usd=50, time_horizon_days=5, reasoning="positive control: known future outcome",
            risk_flags=[], key_uncertainties=[],
        )


def test_engine_captures_a_deliberately_planted_edge(tmp_path):
    """Build a tiny, fully controlled dataset by hand (not the synthetic
    generator) so the "true" resolution is known and fixed: five markets,
    each mispriced by the market at 0.50 (a coin flip) while actually
    guaranteed to resolve a specific way. A strategy that knows the true
    outcome should turn a clear profit."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_date = start + timedelta(days=5)
    truth = {}
    markets_at_t0 = []
    resolutions = {}

    for i in range(5):
        cond = f"KNOWN-{i}"
        outcome = "YES" if i % 2 == 0 else "NO"
        truth[cond] = outcome
        markets_at_t0.append(MarketSnapshot(
            venue="polymarket", condition_id=cond, question=f"Known outcome market {i}", slug=cond,
            yes_token_id=cond, no_token_id=cond, yes_price=0.50, no_price=0.50,
            best_bid_yes=0.49, best_ask_yes=0.51, spread=0.02,
            volume_24hr=50_000, liquidity=20_000, end_date=end_date, tags=[],
        ))
        resolutions[cond] = (outcome, end_date)

    # A second, later tick (past every market's end_date) is required for
    # resolution settlement to actually fire -- _settle_resolutions only
    # checks `resolved_ts <= ts` on ticks the engine actually visits. An
    # empty market list is enough; settlement doesn't need repriced data,
    # just the clock advancing past resolved_ts.
    snapshots = [(start, markets_at_t0), (end_date + timedelta(hours=1), [])]

    cfg = AppConfig()
    cfg.risk.bankroll_usd = 1000.0
    cfg.market_scan.min_volume_24hr = 1000
    cfg.market_scan.min_liquidity = 1000
    engine = BacktestEngine(cfg, _KnownEdgeStrategy(truth), db_path=str(tmp_path / "bt.db"))
    engine.run(snapshots, resolutions)

    report = compute_report(engine.db, cfg.exchange, "known_edge", 1000.0, resolutions)
    assert report.trades_executed == 5
    assert report.trades_closed == 5
    assert report.win_rate == 1.0
    assert report.total_realized_pnl > 0
    assert report.return_pct > 0
    # BUY_NO trades (odd i) were priced at (1 - best_bid_yes) = 0.51 with
    # true_prob = 0.97 -> edge 0.46, same as the BUY_YES trades priced off
    # best_ask_yes = 0.51 -- both sides should show a large, comparable
    # planted edge once avg_edge_at_execution is computed per-side correctly.
    assert report.avg_edge_at_execution is not None
    assert report.avg_edge_at_execution == pytest.approx(0.46, abs=0.01)


def test_avg_edge_at_execution_uses_the_traded_side_price_not_always_the_yes_ask(tmp_path):
    """A bid/ask spread (right at the scanner's max_spread limit, so the
    market still passes filtering) on a pure BUY_NO market makes the bug
    concrete: the risk manager actually priced this trade off
    (1 - best_bid_yes), but the naive abs(model_p - market_price) metric
    always uses best_ask_yes regardless of side -- a materially different
    (and wrong) number here."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_date = start + timedelta(days=5)
    market = MarketSnapshot(
        venue="polymarket", condition_id="SPREAD-NO", question="q", slug="SPREAD-NO",
        yes_token_id="SPREAD-NO", no_token_id="SPREAD-NO", yes_price=0.44, no_price=0.56,
        best_bid_yes=0.40, best_ask_yes=0.48, spread=0.08,
        volume_24hr=50_000, liquidity=20_000, end_date=end_date, tags=[],
    )
    resolutions = {"SPREAD-NO": ("NO", end_date)}
    snapshots = [(start, [market]), (end_date + timedelta(hours=1), [])]

    cfg = AppConfig()
    cfg.risk.bankroll_usd = 1000.0
    cfg.market_scan.min_volume_24hr = 1000
    cfg.market_scan.min_liquidity = 1000
    engine = BacktestEngine(cfg, _KnownEdgeStrategy({"SPREAD-NO": "NO"}), db_path=str(tmp_path / "bt.db"))
    engine.run(snapshots, resolutions)

    report = compute_report(engine.db, cfg.exchange, "known_edge", 1000.0, resolutions)
    assert report.trades_executed == 1
    # Correct: true_prob (0.97) - traded-side price (1 - best_bid_yes = 0.60) = 0.37.
    # The bug's formula would instead give abs(model_p=0.03 - best_ask_yes=0.48) = 0.45.
    assert report.avg_edge_at_execution == pytest.approx(0.37, abs=1e-9)


def test_portfolio_bankroll_does_not_compound_matching_live_resolve_bankroll(tmp_path):
    """BacktestEngine._portfolio_state must hand RiskManager the same FIXED
    bankroll production's resolve_bankroll() would (config.risk.bankroll_usd,
    unchanged by realized P&L) whenever one is configured -- which the
    backtest CLI always does. Compounding here would size trades differently
    than the identical config would size them live."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_date = start + timedelta(days=5)
    market = MarketSnapshot(
        venue="polymarket", condition_id="KNOWN-0", question="q", slug="KNOWN-0",
        yes_token_id="KNOWN-0", no_token_id="KNOWN-0", yes_price=0.50, no_price=0.50,
        best_bid_yes=0.49, best_ask_yes=0.51, spread=0.02,
        volume_24hr=50_000, liquidity=20_000, end_date=end_date, tags=[],
    )
    resolutions = {"KNOWN-0": ("YES", end_date)}
    snapshots = [(start, [market]), (end_date + timedelta(hours=1), [])]

    cfg = AppConfig()
    cfg.risk.bankroll_usd = 1000.0
    cfg.market_scan.min_volume_24hr = 1000
    cfg.market_scan.min_liquidity = 1000
    engine = BacktestEngine(cfg, _KnownEdgeStrategy({"KNOWN-0": "YES"}), db_path=str(tmp_path / "bt.db"))
    engine.run(snapshots, resolutions)

    assert engine.db.get_all_time_realized_pnl(venue=cfg.exchange) > 0
    portfolio = engine._portfolio_state(end_date + timedelta(hours=2))
    assert portfolio.bankroll_usd == engine.starting_bankroll == 1000.0
