"""Configuration loading: config/*.yaml for parameters, environment for secrets.

Secrets are deliberately never read from YAML so that a config file can be
committed or shared without leaking a private key.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class PolymarketConfig(BaseModel):
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 0


class MarketScanConfig(BaseModel):
    max_markets_per_cycle: int = 20
    fetch_pool_size: int = 150
    min_volume_24hr: float = 5000
    min_liquidity: float = 2000
    min_days_to_resolution: float = 0.25
    max_days_to_resolution: float = 45
    min_price: float = 0.03
    max_price: float = 0.97
    max_spread: float = 0.08
    include_tags: List[str] = Field(default_factory=list)
    exclude_tags: List[str] = Field(default_factory=list)


class AIConfig(BaseModel):
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 4000
    min_confidence: float = 0.65
    min_edge: float = 0.05


class RiskConfig(BaseModel):
    bankroll_usd: float = 0
    max_position_usd: float = 25
    max_exposure_per_market_pct: float = 0.10
    max_total_exposure_pct: float = 0.50
    max_open_positions: int = 15
    max_daily_loss_usd: float = 50
    max_daily_trades: int = 20
    kelly_fraction: float = 0.25
    min_order_usd: float = 1
    slippage_bps: int = 100
    take_profit_pct: float = 0.35
    stop_loss_pct: float = 0.35
    exit_before_resolution_hours: float = 2


class LoggingConfig(BaseModel):
    level: str = "INFO"
    db_path: str = "data/polybot.db"
    log_path: str = "data/polybot.log"


class AppConfig(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    polling_interval_seconds: int = 300

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    market_scan: MarketScanConfig = Field(default_factory=MarketScanConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # --- secrets: populated from the environment only, never from YAML ---
    private_key: Optional[str] = None
    funder_address: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    confirm_live: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def can_trade(self) -> bool:
        """Whether we have a signer key at all (dry_run still uses it to read
        balances if present, but doesn't require it)."""
        return bool(self.private_key)


def load_config(path: str | Path = "config/default.yaml") -> AppConfig:
    """Load YAML config, then layer environment-derived secrets/overrides on top."""
    data = {}
    p = Path(path)
    if p.exists():
        with open(p, "r") as f:
            data = yaml.safe_load(f) or {}
    else:
        raise FileNotFoundError(
            f"Config file not found: {p}. Copy config/default.yaml or pass --config."
        )

    cfg = AppConfig(**data)

    cfg.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or None
    cfg.funder_address = os.environ.get("POLYMARKET_FUNDER_ADDRESS") or None
    cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY") or None
    cfg.confirm_live = os.environ.get("POLYBOT_CONFIRM_LIVE", "").strip().lower() == "yes"

    env_mode = os.environ.get("POLYBOT_MODE")
    if env_mode:
        cfg.mode = env_mode  # type: ignore[assignment]

    return cfg
