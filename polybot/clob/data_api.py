"""Thin client for Polymarket's public Data API (no auth required).

Covers on-chain-derived user data: positions and trade history. Used mainly
for reconciliation / status reporting against our own local ledger.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)


class DataApiClient:
    def __init__(self, host: str = "https://data-api.polymarket.com", timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = self.session.get(f"{self.host}{path}", params=clean, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_positions(self, user: str, **params: Any) -> List[Dict[str, Any]]:
        return self._get("/positions", user=user, **params)

    def get_trades(self, user: str, **params: Any) -> List[Dict[str, Any]]:
        return self._get("/trades", user=user, **params)
