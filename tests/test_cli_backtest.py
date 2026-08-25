"""Regression test for `polybot backtest`: the report handed to
compute_report() must describe the bankroll the engine actually sized
positions against, not the raw --starting-bankroll CLI value. These diverge
specifically when --starting-bankroll 0 is passed: BacktestEngine falls back
internally to a nominal $1000 (config.risk.bankroll_usd or 1000.0, mirroring
resolve_bankroll()'s own live-trading fallback), but the CLI used to pass the
original 0 straight through to compute_report() regardless."""
from __future__ import annotations

import json

from click.testing import CliRunner

from polybot.cli import main


def _write_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "exchange: polymarket\n"
        "logging:\n"
        f"  db_path: {tmp_path / 'polybot.db'}\n"
        f"  log_path: {tmp_path / 'polybot.log'}\n"
    )
    return config_path


def test_backtest_report_bankroll_matches_engines_fallback_when_zero_passed(tmp_path):
    config_path = _write_config(tmp_path)
    out_path = tmp_path / "report.json"

    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path), "backtest", "--demo", "--strategy", "null",
            "--starting-bankroll", "0", "--out", str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(out_path.read_text())

    # BacktestEngine.starting_bankroll = config.risk.bankroll_usd or 1000.0
    # -- 0 is falsy, so it falls back to $1000. The report must reflect that
    # same $1000, not the raw 0 that was passed on the command line.
    assert report["starting_bankroll"] == 1000.0
    assert report["ending_bankroll"] == 1000.0  # null strategy: zero trades, zero P&L
    assert report["return_pct"] == 0.0  # would be None if starting_bankroll were still 0
