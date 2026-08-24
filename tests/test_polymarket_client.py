"""PolyTradingClient must not authenticate (a network round-trip to
Polymarket's /auth/api-key) at construction time -- only when a trading
operation that actually needs L2 credentials is invoked. Found via manual
smoke-testing: constructing the client with a wallet key configured used to
hard-fail `polybot scan`/`polybot status` on any transient network issue,
even though neither of those needs write credentials most of the time.
"""
from __future__ import annotations

from eth_account import Account

from polybot.clob.client import PolyTradingClient


def test_construction_does_not_authenticate(monkeypatch):
    from py_clob_client.client import ClobClient

    calls = []
    monkeypatch.setattr(
        ClobClient, "create_or_derive_api_creds",
        lambda self, *a, **kw: calls.append(1) or {"apiKey": "x", "secret": "y", "passphrase": "z"},
    )
    monkeypatch.setattr(ClobClient, "set_api_creds", lambda self, creds: None)

    fake_key = Account.create().key.hex()
    client = PolyTradingClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key=fake_key, funder_address=None, signature_type=0,
    )

    assert calls == []  # constructing the client must not authenticate
    assert client.read_only is False
    assert client._authenticated is False


def test_require_trading_authenticates_exactly_once(monkeypatch):
    from py_clob_client.client import ClobClient

    calls = []
    monkeypatch.setattr(
        ClobClient, "create_or_derive_api_creds",
        lambda self, *a, **kw: calls.append(1) or {"apiKey": "x", "secret": "y", "passphrase": "z"},
    )
    monkeypatch.setattr(ClobClient, "set_api_creds", lambda self, creds: None)

    fake_key = Account.create().key.hex()
    client = PolyTradingClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key=fake_key, funder_address=None, signature_type=0,
    )

    client.require_trading()
    assert calls == [1]
    assert client._authenticated is True

    client.require_trading()  # a second trading call must not re-authenticate
    assert calls == [1]


def test_require_trading_raises_clearly_when_read_only():
    client = PolyTradingClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key=None, funder_address=None, signature_type=0,
    )
    assert client.read_only is True
    try:
        client.require_trading()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "POLYMARKET_PRIVATE_KEY" in str(e)
