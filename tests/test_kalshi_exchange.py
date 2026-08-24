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

from polybot.exchanges.base import LivePosition
from polybot.exchanges.kalshi import API_PREFIX, KalshiExchange, KalshiHttpClient, _to_cents, parse_market


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


# ---- fill-count reconciliation ---------------------------------------------
# Field names (taker_fill_count / maker_fill_count, nested under "order")
# confirmed against Kalshi's own swagger-generated Python SDK model docs
# (github.com/lowgrind/kalshi-python) -- see the module docstring in kalshi.py.


def test_reconcile_fill_reads_taker_fill_count():
    resp = {"order": {"order_id": "abc123", "status": "executed", "taker_fill_count": 5, "maker_fill_count": 0}}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42)
    assert result.success is True
    assert result.filled_shares == 5.0
    assert result.fill_price == 0.42
    assert result.order_id == "abc123"
    assert result.status == "executed"


def test_reconcile_fill_sums_taker_and_maker():
    resp = {"order": {"order_id": "abc123", "status": "executed", "taker_fill_count": 3, "maker_fill_count": 2}}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42)
    assert result.filled_shares == 5.0


def test_reconcile_fill_zero_fill_is_not_success():
    resp = {"order": {"order_id": "abc123", "status": "canceled", "taker_fill_count": 0, "maker_fill_count": 0}}
    result = KalshiExchange._reconcile_fill(resp, requested_count=5, requested_price=0.42)
    assert result.success is False
    assert result.status == "canceled"


def test_reconcile_fill_falls_back_to_requested_count_on_unknown_shape():
    resp = {"order": {"order_id": "abc123", "status": "executed"}}  # neither fill-count field present
    result = KalshiExchange._reconcile_fill(resp, requested_count=7, requested_price=0.5)
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
