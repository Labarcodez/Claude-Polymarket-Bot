"""Configuration loading: config/*.yaml for parameters, environment for secrets.

Secrets are deliberately never read from YAML so that a config file can be
committed or shared without leaking a private key.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr


class PolymarketConfig(BaseModel):
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 0


class KalshiConfig(BaseModel):
    api_host: str = "https://api.elections.kalshi.com"
    demo_host: str = "https://demo-api.kalshi.co"
    use_demo: bool = False
    # Kalshi market status to scan: "open" (tradeable now) is almost always
    # what you want; see GET /markets in Kalshi's API docs for other values.
    status_filter: str = "open"


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
    # Which venue this run trades against. Positions/orders/decisions in the
    # local DB are tagged by venue, so switching this back and forth is safe
    # -- the bot only ever looks at (and risk-manages) the active venue's
    # own open positions.
    exchange: Literal["polymarket", "kalshi"] = "polymarket"
    polling_interval_seconds: int = 300

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    market_scan: MarketScanConfig = Field(default_factory=MarketScanConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # --- secrets: populated from the environment only, never from YAML.
    # SecretStr so an accidental `print(cfg)`, `logger.debug("%s", cfg)`, or
    # a traceback/debugger dump of this object renders as
    # SecretStr('**********') instead of the raw key -- callers that need
    # the actual value must call .get_secret_value() explicitly, which
    # makes "this value might get logged" much harder to do by accident. ---
    private_key: Optional[SecretStr] = None          # Polymarket wallet
    funder_address: Optional[str] = None               # public address, not secret
    anthropic_api_key: Optional[SecretStr] = None
    kalshi_api_key_id: Optional[SecretStr] = None
    kalshi_private_key_pem: Optional[SecretStr] = None
    confirm_live: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def can_trade(self) -> bool:
        """Whether we have credentials for the *active* exchange (dry_run
        still uses them to read balances if present, but doesn't require them)."""
        if self.exchange == "kalshi":
            return bool(self.kalshi_api_key_id and self.kalshi_private_key_pem)
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

    cfg.private_key = _secret(os.environ.get("POLYMARKET_PRIVATE_KEY"))
    cfg.funder_address = os.environ.get("POLYMARKET_FUNDER_ADDRESS") or None
    cfg.anthropic_api_key = _secret(os.environ.get("ANTHROPIC_API_KEY"))
    cfg.kalshi_api_key_id = _secret(os.environ.get("KALSHI_API_KEY_ID"))
    cfg.kalshi_private_key_pem = _secret(_load_kalshi_private_key())
    cfg.confirm_live = os.environ.get("POLYBOT_CONFIRM_LIVE", "").strip().lower() == "yes"

    # Direct attribute assignment on a pydantic model does NOT re-run the
    # field's Literal validation (that only happens at construction time),
    # so these two env overrides are validated by hand -- an invalid value
    # here must raise loudly rather than silently taking effect as a
    # nonsense string that then does the wrong thing quietly (e.g.
    # `POLYBOT_MODE=Live` would otherwise leave `cfg.mode` as the raw
    # string "Live", `is_live` would evaluate False since it's not exactly
    # "live", and the bot would silently stay in dry_run while `polybot
    # status` prints the misleading "Mode: Live").
    env_mode = os.environ.get("POLYBOT_MODE")
    if env_mode:
        if env_mode not in ("dry_run", "live"):
            raise ValueError(f"POLYBOT_MODE must be 'dry_run' or 'live', got {env_mode!r}")
        cfg.mode = env_mode  # type: ignore[assignment]

    env_exchange = os.environ.get("POLYBOT_EXCHANGE")
    if env_exchange:
        if env_exchange not in ("polymarket", "kalshi"):
            raise ValueError(f"POLYBOT_EXCHANGE must be 'polymarket' or 'kalshi', got {env_exchange!r}")
        cfg.exchange = env_exchange  # type: ignore[assignment]

    return cfg


def _secret(value: Optional[str]) -> Optional[SecretStr]:
    return SecretStr(value) if value else None


def unwrap_secret(value: Optional[SecretStr]) -> Optional[str]:
    """The one place callers should reach for the raw value of a SecretStr
    config field -- makes every such access grep-able and deliberate."""
    return value.get_secret_value() if value else None


def _load_kalshi_private_key() -> Optional[str]:
    """Kalshi's RSA private key can be supplied either inline
    (KALSHI_PRIVATE_KEY, a PEM string -- handy for a secrets manager /
    single-line-escaped env value) or as a path to a PEM file
    (KALSHI_PRIVATE_KEY_PATH, the more common local-dev setup)."""
    inline = os.environ.get("KALSHI_PRIVATE_KEY")
    if inline:
        return inline.replace("\\n", "\n")

    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if path and Path(path).exists():
        return Path(path).read_text()

    return None
