from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from polybot.exchanges.polymarket import parse_market


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
