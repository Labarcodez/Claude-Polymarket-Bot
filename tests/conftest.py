from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybot.config import AIConfig, MarketScanConfig, RiskConfig
from polybot.engine.models import MarketSnapshot


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        bankroll_usd=0,
        max_position_usd=25,
        max_exposure_per_market_pct=0.10,
        max_total_exposure_pct=0.50,
        max_open_positions=15,
        max_daily_loss_usd=50,
        max_daily_trades=20,
        kelly_fraction=0.25,
        min_order_usd=1,
        slippage_bps=100,
        take_profit_pct=0.35,
        stop_loss_pct=0.35,
        exit_before_resolution_hours=2,
    )


@pytest.fixture
def ai_config() -> AIConfig:
    return AIConfig(min_confidence=0.65, min_edge=0.05)


@pytest.fixture
def scan_config() -> MarketScanConfig:
    return MarketScanConfig(
        max_markets_per_cycle=20,
        fetch_pool_size=150,
        min_volume_24hr=5000,
        min_liquidity=2000,
        min_days_to_resolution=0.25,
        max_days_to_resolution=45,
        min_price=0.03,
        max_price=0.97,
        max_spread=0.08,
    )


@pytest.fixture
def sample_market() -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="0xcond123",
        question="Will it rain tomorrow?",
        slug="will-it-rain-tomorrow",
        description="Resolves YES if it rains.",
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
        tags=["Weather"],
    )
