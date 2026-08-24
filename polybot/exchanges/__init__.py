"""Exchange adapters: one module per prediction-market venue, all implementing
the common `ExchangeAdapter` interface in base.py so the rest of the bot
(engine/, risk/) never needs to know which venue it's actually trading on.
"""
from __future__ import annotations

from ..config import AppConfig
from .base import ExchangeAdapter


def build_exchange(config: AppConfig) -> ExchangeAdapter:
    """Factory: construct the ExchangeAdapter selected by config.exchange."""
    if config.exchange == "polymarket":
        from .polymarket import PolymarketExchange

        return PolymarketExchange(config)
    elif config.exchange == "kalshi":
        from .kalshi import KalshiExchange

        return KalshiExchange(config)
    raise ValueError(f"Unknown exchange: {config.exchange!r} (expected 'polymarket' or 'kalshi')")
