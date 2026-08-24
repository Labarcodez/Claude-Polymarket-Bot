"""End-to-end test of TradingEngine.run_cycle in dry_run mode, against a
fake in-memory ExchangeAdapter and a monkeypatched ClaudeAnalyst -- no
network access, no real credentials. This is the one thing the rest of the
suite doesn't cover: that scanner -> analyst -> risk manager -> db actually
wire together correctly end to end, not just in isolation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from polybot.ai.analyst import ClaudeAnalyst
from polybot.config import AppConfig, LoggingConfig
from polybot.engine.models import Action, MarketSnapshot, OpenPosition, OrderPlan, TradeDecision
from polybot.exchanges.base import ExchangeAdapter, ExecutionResult


class FakeExchange(ExchangeAdapter):
    """A fully in-memory stand-in for a real venue. Records every call to
    execute_entry/execute_exit so tests can assert dry_run never reaches
    them (simulation is centralized in TradingEngine/ExitManager, not the
    adapter)."""

    name = "polymarket"

    def __init__(self, markets: List[MarketSnapshot], midpoints: Optional[dict] = None):
        self._markets = markets
        self._midpoints = midpoints or {}
        self.entry_calls: List[OrderPlan] = []
        self.exit_calls: List[OpenPosition] = []

    @property
    def read_only(self) -> bool:
        return True

    def require_trading(self) -> None:
        raise RuntimeError("read-only fake exchange")

    def fetch_raw_markets(self, pool_size: int) -> List[MarketSnapshot]:
        return list(self._markets)

    def get_midpoint(self, market_ref: str, outcome: str) -> Optional[float]:
        return self._midpoints.get((market_ref, outcome), self._midpoints.get(market_ref))

    def get_balance_usd(self) -> Optional[float]:
        return None

    def execute_entry(self, plan: OrderPlan) -> ExecutionResult:
        self.entry_calls.append(plan)
        raise AssertionError("execute_entry must not be called in dry_run")

    def execute_exit(self, position: OpenPosition, price_hint: float) -> ExecutionResult:
        self.exit_calls.append(position)
        raise AssertionError("execute_exit must not be called in dry_run")


def make_market(**overrides) -> MarketSnapshot:
    base = dict(
        venue="polymarket",
        condition_id="0xcond-e2e",
        question="Will the end-to-end test pass?",
        slug="e2e-test-market",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        yes_price=0.40,
        no_price=0.60,
        best_bid_yes=0.39,
        best_ask_yes=0.41,
        spread=0.02,
        tick_size=0.01,
        volume_24hr=50_000,
        liquidity=20_000,
        end_date=datetime.now(timezone.utc) + timedelta(days=5),
        tags=[],
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def make_decision(**overrides) -> TradeDecision:
    base = dict(
        action=Action.BUY_YES, fair_value_probability=0.70, confidence=0.85,
        suggested_size_usd=200, time_horizon_days=5, reasoning="strong edge",
        risk_flags=[], key_uncertainties=[],
    )
    base.update(overrides)
    return TradeDecision(**base)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from polybot.engine import trader as trader_module

    market = make_market()
    fake_exchange = FakeExchange([market])
    monkeypatch.setattr(trader_module, "build_exchange", lambda cfg: fake_exchange)
    monkeypatch.setattr(ClaudeAnalyst, "analyze", lambda self, m: make_decision())

    cfg = AppConfig(
        mode="dry_run", exchange="polymarket",
        anthropic_api_key="sk-test-dummy",
        logging=LoggingConfig(db_path=str(tmp_path / "e2e.db"), log_path=str(tmp_path / "e2e.log")),
    )
    eng = trader_module.TradingEngine(cfg)
    eng.fake_exchange = fake_exchange  # stash for assertions
    return eng


def test_run_cycle_dry_run_opens_a_simulated_position(engine):
    engine.run_cycle()

    positions = engine.db.get_open_positions(venue="polymarket")
    assert len(positions) == 1
    assert positions[0].outcome == "YES"
    assert positions[0].condition_id == "0xcond-e2e"
    assert positions[0].shares > 0

    # dry_run must never touch the adapter's live order-execution path.
    assert engine.fake_exchange.entry_calls == []

    stats = engine.db.get_today_stats(venue="polymarket")
    assert stats["trades_count"] == 1


def test_run_cycle_does_not_reenter_a_market_it_already_holds(engine, monkeypatch):
    engine.run_cycle()
    assert len(engine.db.get_open_positions(venue="polymarket")) == 1

    # Second cycle: same market, same bullish decision -- risk manager
    # should refuse a second entry into a market we already hold.
    engine.run_cycle()
    positions = engine.db.get_open_positions(venue="polymarket")
    assert len(positions) == 1  # unchanged, not doubled


def test_run_cycle_skips_low_confidence_decisions(tmp_path, monkeypatch):
    from polybot.engine import trader as trader_module

    market = make_market(condition_id="0xlowconf")
    fake_exchange = FakeExchange([market])
    monkeypatch.setattr(trader_module, "build_exchange", lambda cfg: fake_exchange)
    monkeypatch.setattr(ClaudeAnalyst, "analyze", lambda self, m: make_decision(confidence=0.10))

    cfg = AppConfig(
        mode="dry_run", exchange="polymarket", anthropic_api_key="sk-test-dummy",
        logging=LoggingConfig(db_path=str(tmp_path / "e2e2.db"), log_path=str(tmp_path / "e2e2.log")),
    )
    eng = trader_module.TradingEngine(cfg)
    eng.run_cycle()

    assert eng.db.get_open_positions(venue="polymarket") == []


def test_run_cycle_closes_a_position_hitting_take_profit(engine):
    engine.run_cycle()
    [pos] = engine.db.get_open_positions(venue="polymarket")

    # Move the market's price far above cost -- comfortably past the default
    # take_profit_pct -- consistently in *both* places a real order book
    # would move together: the midpoint ExitManager reads, and the
    # scanner's own snapshot price. (Only bumping the midpoint would leave
    # the scanner still quoting the old, cheap price on this same cycle's
    # fresh scan, which -- against a stub analyst that always recommends
    # the same BUY_YES regardless of price -- would make the bot
    # immediately buy right back in and defeat the point of this test:
    # verifying the take-profit exit itself, not cycle-level re-entry
    # behavior.)
    new_price = min(pos.avg_cost * 2.0, 0.99)
    engine.fake_exchange._midpoints[pos.token_id] = new_price
    market = engine.fake_exchange._markets[0]
    market.yes_price = new_price
    market.best_ask_yes = new_price
    market.best_bid_yes = new_price

    engine.run_cycle()

    assert engine.db.get_open_positions(venue="polymarket") == []
    stats = engine.db.get_today_stats(venue="polymarket")
    assert stats["realized_pnl"] > 0


# ---- resolve_bankroll: single source of truth shared by TradingEngine and `polybot status` ----


class _ReadWriteFakeExchange(FakeExchange):
    """Same as FakeExchange but reports credentials as present, so
    resolve_bankroll's live-balance branch is reachable in tests."""

    def __init__(self, balance):
        super().__init__(markets=[])
        self._balance = balance

    @property
    def read_only(self) -> bool:
        return False

    def get_balance_usd(self) -> Optional[float]:
        return self._balance


def test_resolve_bankroll_prefers_fixed_config_value():
    from polybot.config import AppConfig, RiskConfig
    from polybot.engine.trader import resolve_bankroll

    cfg = AppConfig(risk=RiskConfig(bankroll_usd=500))
    bankroll, note = resolve_bankroll(cfg, FakeExchange(markets=[]), dry_run=True)
    assert bankroll == 500
    assert "config" in note


def test_resolve_bankroll_reads_live_balance_when_live_and_not_read_only():
    from polybot.config import AppConfig
    from polybot.engine.trader import resolve_bankroll

    cfg = AppConfig()  # risk.bankroll_usd defaults to 0 -> not fixed
    bankroll, note = resolve_bankroll(cfg, _ReadWriteFakeExchange(balance=321.50), dry_run=False)
    assert bankroll == 321.50
    assert "live" in note.lower()


def test_resolve_bankroll_treats_a_failed_live_fetch_as_zero_not_the_nominal_fallback():
    from polybot.config import AppConfig
    from polybot.engine.trader import resolve_bankroll

    cfg = AppConfig()
    bankroll, _note = resolve_bankroll(cfg, _ReadWriteFakeExchange(balance=None), dry_run=False)
    assert bankroll == 0.0


def test_resolve_bankroll_falls_back_to_nominal_paper_bankroll_in_dry_run():
    from polybot.config import AppConfig
    from polybot.engine.trader import resolve_bankroll

    cfg = AppConfig()
    bankroll, note = resolve_bankroll(cfg, FakeExchange(markets=[]), dry_run=True)
    assert bankroll == 1000.0
    assert "nominal" in note.lower()
