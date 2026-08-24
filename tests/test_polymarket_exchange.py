from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from polybot.exchanges.polymarket import PolymarketExchange, parse_market


def raw_market(**overrides):
    base = {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "description": "desc",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.35", "0.65"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "bestBid": "0.34",
        "bestAsk": "0.36",
        "spread": "0.02",
        "volume24hr": "10000",
        "liquidity": "5000",
        "endDate": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        "enableOrderBook": True,
        "negRisk": False,
        "tags": [{"label": "Politics"}],
    }
    base.update(overrides)
    return base


def test_parse_market_happy_path():
    snap = parse_market(raw_market())
    assert snap is not None
    assert snap.venue == "polymarket"
    assert snap.condition_id == "0xabc"
    assert snap.yes_token_id == "tok-yes"
    assert snap.no_token_id == "tok-no"
    assert snap.yes_price == 0.35
    assert snap.no_price == 0.65
    assert snap.tags == ["Politics"]


def test_parse_market_handles_already_parsed_lists():
    m = raw_market(outcomes=["Yes", "No"], outcomePrices=[0.4, 0.6], clobTokenIds=["a", "b"])
    snap = parse_market(m)
    assert snap is not None
    assert snap.yes_price == 0.4


def test_parse_market_rejects_non_binary():
    m = raw_market(outcomes=json.dumps(["A", "B", "C"]), clobTokenIds=json.dumps(["a", "b", "c"]))
    assert parse_market(m) is None


def test_parse_market_rejects_order_book_disabled():
    assert parse_market(raw_market(enableOrderBook=False)) is None


# ---- reconciliation (get_live_positions) -----------------------------------
# Response field names (conditionId, size, outcome, title) confirmed against
# Polymarket's documented Data API /positions endpoint -- see the docstring
# on PolymarketExchange.get_live_positions.


def _bare_polymarket_exchange() -> PolymarketExchange:
    """Construct a PolymarketExchange without going through __init__ (which
    needs a full AppConfig + live py-clob-client construction) -- just
    enough to exercise get_live_positions."""
    exchange = PolymarketExchange.__new__(PolymarketExchange)
    exchange.data_api = MagicMock()
    exchange.clob = MagicMock()
    exchange.clob.require_trading.return_value = None
    exchange._funder_address = "0xFUNDER"
    return exchange


def test_get_live_positions_parses_data_api_response():
    exchange = _bare_polymarket_exchange()
    exchange.data_api.get_positions.return_value = [
        {"conditionId": "0xabc", "size": 12.5, "outcome": "Yes", "title": "Will X happen?"},
        {"conditionId": "0xdef", "size": 3.0, "outcome": "No", "title": "Will Y happen?"},
    ]

    positions = exchange.get_live_positions()

    exchange.data_api.get_positions.assert_called_once_with(user="0xFUNDER", sizeThreshold=0)
    by_id = {p.condition_id: p for p in positions}
    assert by_id["0xabc"].shares == 12.5
    assert by_id["0xabc"].outcome == "YES"
    assert by_id["0xabc"].question == "Will X happen?"
    assert by_id["0xdef"].outcome == "NO"


def test_get_live_positions_returns_none_on_failure():
    exchange = _bare_polymarket_exchange()
    exchange.data_api.get_positions.side_effect = RuntimeError("network error")
    assert exchange.get_live_positions() is None


def test_get_live_positions_falls_back_to_signer_address_without_funder():
    exchange = _bare_polymarket_exchange()
    exchange._funder_address = None
    exchange.clob.client.get_address.return_value = "0xSIGNER"
    exchange.data_api.get_positions.return_value = []

    exchange.get_live_positions()

    exchange.data_api.get_positions.assert_called_once_with(user="0xSIGNER", sizeThreshold=0)
