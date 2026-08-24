"""Secrets in AppConfig must never render in plain text via repr/str -- the
whole point of using pydantic.SecretStr for them. This guards against a
future `logger.debug("%s", cfg)` or an exception/traceback dump leaking a
private key or API key."""
from __future__ import annotations

import os

from polybot.config import AppConfig, load_config, unwrap_secret


def test_secrets_are_redacted_in_str_and_repr():
    cfg = AppConfig(
        private_key="0xSUPERSECRETKEY",
        anthropic_api_key="sk-ant-realkey123",
        kalshi_api_key_id="kalshi-key-id",
        kalshi_private_key_pem="-----BEGIN PRIVATE KEY-----\nMII...\n-----END PRIVATE KEY-----",
    )

    dump = str(cfg) + repr(cfg)

    for secret in ("0xSUPERSECRETKEY", "sk-ant-realkey123", "kalshi-key-id", "BEGIN PRIVATE KEY"):
        assert secret not in dump, f"{secret!r} leaked into str/repr(cfg)"


def test_unwrap_secret_recovers_the_real_value():
    cfg = AppConfig(private_key="0xSUPERSECRETKEY")
    assert unwrap_secret(cfg.private_key) == "0xSUPERSECRETKEY"
    assert unwrap_secret(None) is None


def test_funder_address_is_not_secret_and_stays_plain():
    # It's a public on-chain address, not a credential -- should remain a
    # plain str (not wrapped/redacted) so it's easy to display in `status`.
    cfg = AppConfig(funder_address="0xPublicAddress")
    assert cfg.funder_address == "0xPublicAddress"
    assert isinstance(cfg.funder_address, str)


def test_load_config_rejects_an_invalid_polybot_mode(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("exchange: polymarket\n")
    monkeypatch.setenv("POLYBOT_MODE", "Live")  # wrong case -- must not silently mean dry_run
    for var in ("POLYMARKET_PRIVATE_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    try:
        load_config(config_path)
        assert False, "expected ValueError for an invalid POLYBOT_MODE"
    except ValueError as e:
        assert "POLYBOT_MODE" in str(e)


def test_load_config_rejects_an_invalid_polybot_exchange(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("exchange: polymarket\n")
    monkeypatch.setenv("POLYBOT_EXCHANGE", "coinbase")

    try:
        load_config(config_path)
        assert False, "expected ValueError for an invalid POLYBOT_EXCHANGE"
    except ValueError as e:
        assert "POLYBOT_EXCHANGE" in str(e)


def test_load_config_wraps_env_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("exchange: polymarket\n")

    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xFROMENV")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv")
    for var in ("POLYMARKET_FUNDER_ADDRESS", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)

    cfg = load_config(config_path)

    assert unwrap_secret(cfg.private_key) == "0xFROMENV"
    assert unwrap_secret(cfg.anthropic_api_key) == "sk-ant-fromenv"
    assert "0xFROMENV" not in repr(cfg.private_key)
