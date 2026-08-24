"""Tests for the Kalshi adapter's pure logic: market parsing and RSA-PSS
request signing. The signing test generates its own throwaway keypair and
verifies the signature against Kalshi's documented message format
(timestamp_ms + METHOD + path) -- no network access or real Kalshi
credentials required."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from polybot.engine.models import OpenPosition, OrderPlan
from polybot.exchanges.base import LivePosition
from polybot.exchanges.kalshi import (
    API_PREFIX,
    KalshiExchange,
    KalshiHttpClient,
    _book_side,
    _to_cents,
    _to_price_string,
    _yes_equivalent_price,
    parse_market,
)


def raw_market(**overrides):
    base = {
        "ticker": "KXTEST-24-YES",
        "title": "Will X happen?",
        "subtitle": "Resolves YES if X happens.",
        "category": "Politics",
        "status": "open",
        "yes_bid": 34,
        "yes_ask": 36,
        "no_bid": 64,
        "no_ask": 66,
        "last_price": 35,
        "volume_24h": 1200,
        "open_interest": 4000,
        "close_time": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
    }
    base.update(overrides)
    return base


def test_parse_market_happy_path():
    snap = parse_market(raw_market())
    assert snap is not None
    assert snap.venue == "kalshi"
    assert snap.condition_id == "KXTEST-24-YES"
    # Kalshi addresses orders by ticker + side, not per-outcome ids.
    assert snap.yes_token_id == snap.no_token_id == "KXTEST-24-YES"
    assert snap.yes_price == pytest.approx(0.36)  # best ask, cents -> probability
    assert snap.no_price == pytest.approx(0.64)
    assert snap.best_bid_yes == pytest.approx(0.34)
    assert snap.best_ask_yes == pytest.approx(0.36)
    assert snap.spread == pytest.approx(0.02)
    assert snap.tags == ["Politics"]


def test_parse_market_rejects_missing_ticker():
    m = raw_market()
    del m["ticker"]
    assert parse_market(m) is None


def test_parse_market_falls_back_to_last_price_without_ask():
    m = raw_market(yes_ask=None)
    snap = parse_market(m)
    assert snap is not None
    assert snap.yes_price == pytest.approx(0.35)  # last_price


def test_parse_market_returns_none_without_any_usable_price():
    m = raw_market(yes_bid=None, yes_ask=None, last_price=None)
    assert parse_market(m) is None


def test_to_cents_clamps_and_rounds():
    assert _to_cents(0.5) == 50
    assert _to_cents(0.001) == 1   # clamped up to the 1-cent floor
    assert _to_cents(0.999) == 99  # clamped down to the 99-cent ceiling
    assert _to_cents(0.4321) == 43


# ---- RSA-PSS request signing ------------------------------------------------


@pytest.fixture
def rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_key, pem


def test_can_sign_false_without_credentials():
    client = KalshiHttpClient("https://example.invalid", api_key_id=None, private_key_pem=None)
    assert client.can_sign is False
    assert client._auth_headers("GET", "/trade-api/v2/portfolio/balance") == {}


def test_auth_headers_signature_verifies_against_public_key(rsa_keypair):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key, pem = rsa_keypair
    client = KalshiHttpClient("https://example.invalid", api_key_id="test-key-id", private_key_pem=pem)
    assert client.can_sign is True

    path = f"{API_PREFIX}/portfolio/orders"
    headers = client._auth_headers("POST", path)

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()

    message = f"{headers['KALSHI-ACCESS-TIMESTAMP']}POST{path}".encode("utf-8")
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Should not raise: proves the signature really was produced by this
    # keypair over exactly this message, matching Kalshi's documented format.
    private_key.public_key().verify(
        signature, message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )

    # A tampered message must NOT verify.
    with pytest.raises(InvalidSignature):
        private_key.public_key().verify(
            signature, message + b"tampered",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )


# ---- V2 price/side conversion helpers ---------------------------------------
# The V2 order API's `price` field is always the YES-contract-equivalent
# price regardless of book side; `_yes_equivalent_price`/`_book_side` are
# the one place that conversion happens -- see the module docstring in
# kalshi.py for the full reasoning and sourcing.


def test_yes_equivalent_price_is_identity_for_yes():
    assert _yes_equivalent_price("YES", 0.42) == 0.42


def test_yes_equivalent_price_complements_for_no():
    # Paying $0.83 for NO corresponds to a YES-equivalent price of $0.17.
    assert _yes_equivalent_price("NO", 0.83) == pytest.approx(0.17)


def test_yes_equivalent_price_is_self_inverse():
    for outcome in ("YES", "NO"):
        price = 0.37
        assert _yes_equivalent_price(outcome, _yes_equivalent_price(outcome, price)) == pytest.approx(price)


def test_book_side_bid_for_yes_ask_for_no():
    assert _book_side("YES") == "bid"
    assert _book_side("NO") == "ask"


def test_to_price_string_formats_as_fixed_point_dollars():
    assert _to_price_string(0.62) == "0.6200"
    assert _to_price_string(0.01) == "0.0100"
    assert _to_price_string(0.4321) == "0.4300"  # rounds to the nearest cent first


# ---- order body shape (execute_entry / execute_exit) -----------------------


def _bare_trading_kalshi_exchange() -> KalshiExchange:
    exchange = KalshiExchange.__new__(KalshiExchange)
    exchange.http = MagicMock()
    exchange.http.can_sign = True
    return exchange


def _make_plan(**overrides):
    base = dict(
        venue="kalshi", condition_id="KXTEST-24", token_id="KXTEST-24", question="Will X happen?",
        outcome="YES", side="BUY", size_usd=10.0, limit_price=0.42, decision_confidence=0.8,
        fair_value_probability=0.6, reasoning="test",
    )
    base.update(overrides)
    return OrderPlan(**base)


def test_execute_entry_posts_to_v2_endpoint_with_bid_side_for_yes():
    exchange = _bare_trading_kalshi_exchange()
    exchange.http.post.return_value = {"order_id": "o1", "status": "executed", "fill_count": 23}

    plan = _make_plan(outcome="YES", limit_price=0.40, size_usd=20.0, token_id="KXTEST-24")
    result = exchange.execute_entry(plan)

    path, body = exchange.http.post.call_args[0]
    assert path == f"{API_PREFIX}/portfolio/events/orders"
    assert body["ticker"] == "KXTEST-24"
    assert body["side"] == "bid"
    assert body["price"] == "0.4000"  # YES limit_price unchanged for a YES order
    assert body["count"] == "50"  # $20 / $0.40 = 50 contracts, formatted as a string
    assert body["time_in_force"] == "fill_or_kill"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert "reduce_only" not in body  # never set on an opening order
    assert isinstance(body["client_order_id"], str) and len(body["client_order_id"]) > 0
    assert result.success is True
    assert result.filled_shares == 23.0


def test_execute_entry_posts_ask_side_and_converts_price_for_no():
    exchange = _bare_trading_kalshi_exchange()
    exchange.http.post.return_value = {"order_id": "o1", "status": "executed", "fill_count": 16}

    # Buying NO at up to $0.62 -> YES-equivalent price is 1 - 0.62 = 0.38.
    plan = _make_plan(outcome="NO", limit_price=0.62, size_usd=10.0)
    exchange.execute_entry(plan)

    _path, body = exchange.http.post.call_args[0]
    assert body["side"] == "ask"
    assert body["price"] == "0.3800"


def test_execute_exit_closes_a_yes_position_with_reduce_only_ask():
    exchange = _bare_trading_kalshi_exchange()
    exchange.http.post.return_value = {"order_id": "o2", "status": "executed", "fill_count": 5}

    pos = OpenPosition(
        venue="kalshi", condition_id="KXTEST-24", token_id="KXTEST-24", outcome="YES",
        question="Will X happen?", shares=5, avg_cost=0.40, opened_ts=datetime.now(timezone.utc),
    )
    result = exchange.execute_exit(pos, price_hint=0.80)

    path, body = exchange.http.post.call_args[0]
    assert path == f"{API_PREFIX}/portfolio/events/orders"
    assert body["ticker"] == "KXTEST-24"
    assert body["count"] == "5"
    assert body["side"] == "ask"  # opposite of the "bid" that opened a YES position
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "fill_or_kill"
    # floor = 0.80 * 0.95 = 0.76, YES-equivalent unchanged for a YES position
    assert body["price"] == "0.7600"
    assert result.success is True
    assert result.filled_shares == 5.0


def test_execute_exit_closes_a_no_position_with_reduce_only_bid():
    exchange = _bare_trading_kalshi_exchange()
    exchange.http.post.return_value = {"order_id": "o3", "status": "executed", "fill_count": 5}

    pos = OpenPosition(
        venue="kalshi", condition_id="KXTEST-24", token_id="KXTEST-24", outcome="NO",
        question="Will X happen?", shares=5, avg_cost=0.60, opened_ts=datetime.now(timezone.utc),
    )
    exchange.execute_exit(pos, price_hint=0.60)

    _path, body = exchange.http.post.call_args[0]
    assert body["side"] == "bid"  # opposite of the "ask" that opened a NO position
    assert body["reduce_only"] is True
    # floor = 0.60 * 0.95 = 0.57 (in NO terms) -> YES-equivalent = 1 - 0.57 = 0.43
    assert body["price"] == "0.4300"


def test_execute_exit_close_side_is_always_the_opposite_of_book_side():
    # Pins the entry/exit relationship itself (not just today's bid/ask
    # values) so the two can never silently drift apart -- see
    # execute_exit's derivation of close_side from _book_side().
    for outcome in ("YES", "NO"):
        exchange = _bare_trading_kalshi_exchange()
        exchange.http.post.return_value = {"order_id": "o", "status": "executed", "fill_count": 1}
        pos = OpenPosition(
            venue="kalshi", condition_id="KXTEST-24", token_id="KXTEST-24", outcome=outcome,
            question="Will X happen?", shares=1, avg_cost=0.50, opened_ts=datetime.now(timezone.utc),
        )
        exchange.execute_exit(pos, price_hint=0.50)
        _path, body = exchange.http.post.call_args[0]
        assert body["side"] != _book_side(outcome)


# ---- fill-count reconciliation ---------------------------------------------
# Confirmed against the raw V2 spec quote in a real integration's issue
# history (see the module docstring in kalshi.py): POST
# /portfolio/events/orders returns a thin ack with `fill_count`, not the
# legacy `taker_fill_count`/`maker_fill_count` pair.


def test_reconcile_fill_reads_fill_count():
    resp = {"order_id": "abc123", "status": "executed", "fill_count": 5, "remaining_count": 0}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42, outcome="YES")
    assert result.success is True
    assert result.filled_shares == 5.0
    assert result.fill_price == 0.42
    assert result.order_id == "abc123"
    assert result.status == "executed"


def test_reconcile_fill_uses_average_fill_price_converted_from_yes_equivalent():
    # average_fill_price is YES-equivalent; for a NO order it must be
    # converted back into NO terms for the ledger.
    resp = {"order_id": "abc123", "status": "executed", "fill_count": 5, "average_fill_price": "0.35"}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.62, outcome="NO")
    assert result.fill_price == pytest.approx(0.65)  # 1 - 0.35


def test_reconcile_fill_falls_back_to_legacy_taker_maker_fields():
    resp = {"order": {"order_id": "abc123", "status": "executed", "taker_fill_count": 3, "maker_fill_count": 2}}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42, outcome="YES")
    assert result.filled_shares == 5.0


def test_reconcile_fill_zero_fill_is_not_success():
    resp = {"order_id": "abc123", "status": "canceled", "fill_count": 0}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42, outcome="YES")
    assert result.success is False
    assert result.status == "canceled"


def test_reconcile_fill_falls_back_to_requested_count_on_unknown_shape():
    resp = {"order_id": "abc123", "status": "executed"}  # no fill-count field at all
    result = KalshiExchange._reconcile_fill(resp, requested_count=7, requested_price=0.5, outcome="YES")
    assert result.success is True
    assert result.filled_shares == 7.0  # optimistic fallback, not silently dropped


# ---- reconciliation (get_live_positions) -----------------------------------


def _bare_kalshi_exchange() -> KalshiExchange:
    """Construct a KalshiExchange without going through __init__ (which
    needs a full AppConfig) -- just enough to exercise get_live_positions."""
    exchange = KalshiExchange.__new__(KalshiExchange)
    exchange.http = MagicMock()
    exchange.http.can_sign = True
    return exchange


def test_get_live_positions_parses_signed_position_field():
    exchange = _bare_kalshi_exchange()
    exchange.http.get.return_value = {
        "market_positions": [
            {"ticker": "KXYES-24", "position": 12},   # net long YES
            {"ticker": "KXNO-24", "position": -8},     # net long NO
            {"ticker": "KXFLAT-24", "position": 0},    # flat -- should be skipped
        ]
    }
    positions = exchange.get_live_positions()
    by_ticker = {p.condition_id: p for p in positions}

    assert by_ticker["KXYES-24"] == LivePosition(condition_id="KXYES-24", outcome="YES", shares=12.0, question="KXYES-24")
    assert by_ticker["KXNO-24"].outcome == "NO"
    assert by_ticker["KXNO-24"].shares == 8.0
    assert "KXFLAT-24" not in by_ticker


def test_get_live_positions_returns_none_on_failure():
    exchange = _bare_kalshi_exchange()
    exchange.http.get.side_effect = RuntimeError("network error")
    assert exchange.get_live_positions() is None
