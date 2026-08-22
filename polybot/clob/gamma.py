"""Thin client for Polymarket's public Gamma API (no auth required).

Used for market/event discovery. The exact response schema of the Gamma API
is not versioned or formally documented and can drift -- see
`polybot inspect-market <slug>` for a way to dump a raw market payload and
sanity-check the field names this module relies on (see engine/scanner.py)
against what the live API is actually returning.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)


class GammaClient:
    def __init__(self, host: str = "https://gamma-api.polymarket.com", timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, **params: Any) -> Any:
        # Drop None values so we don't send e.g. tag_id=None as a literal string.
        clean = {k: v for k, v in params.items() if v is not None}
        resp = self.session.get(f"{self.host}{path}", params=clean, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_markets(self, **params: Any) -> List[Dict[str, Any]]:
        """GET /markets -- flat list of markets (each market = one binary/scalar question)."""
        data = self._get("/markets", **params)
        return data if isinstance(data, list) else data.get("data", [])

    def get_market_by_slug(self, slug: str) -> Dict[str, Any]:
        results = self.get_markets(slug=slug)
        if not results:
            raise LookupError(f"No market found for slug={slug!r}")
        return results[0]

    def get_events(self, **params: Any) -> List[Dict[str, Any]]:
        """GET /events -- events group related markets (e.g. multi-outcome elections)."""
        data = self._get("/events", **params)
        return data if isinstance(data, list) else data.get("data", [])
