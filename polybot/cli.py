"""Command-line entry point: `polybot <command>`."""
from __future__ import annotations

import logging
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .config import AppConfig, load_config
from .logging_setup import setup_logging

console = Console()
logger = logging.getLogger("polybot.cli")


def _load(config_path: str, exchange_override: Optional[str] = None) -> AppConfig:
    load_dotenv()
    cfg = load_config(config_path)
    if exchange_override:
        cfg.exchange = exchange_override  # type: ignore[assignment]
    setup_logging(cfg.logging.level, cfg.logging.log_path)
    return cfg


def _guard_live(cfg: AppConfig) -> None:
    if cfg.is_live and not cfg.confirm_live:
        console.print(
            "[bold red]Refusing to trade live:[/bold red] config mode is 'live' but "
            "POLYBOT_CONFIRM_LIVE=yes is not set in your environment.\n"
            "This is an intentional second safety gate -- see .env.example and "
            "docs/RISK_DISCLAIMER.md before enabling it."
        )
        raise SystemExit(1)
    if cfg.is_live and not cfg.can_trade:
        console.print(
            f"[bold red]Refusing to trade live:[/bold red] no trading credentials set "
            f"for exchange={cfg.exchange!r}. See .env.example."
        )
        raise SystemExit(1)


@click.group()
@click.option("--config", "config_path", default="config/default.yaml", show_default=True, help="Path to config YAML.")
@click.option(
    "--exchange", "exchange_override", type=click.Choice(["polymarket", "kalshi"]), default=None,
    help="Override config.exchange for this invocation.",
)
@click.pass_context
def main(ctx: click.Context, config_path: str, exchange_override: Optional[str]) -> None:
    """Claude-Polymarket-Bot: an AI-analyzed, risk-managed trading bot for
    Polymarket and Kalshi prediction markets.

    Trading involves real financial risk. Start with `mode: dry_run` (the
    default) and read docs/RISK_DISCLAIMER.md before ever setting mode: live.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["exchange_override"] = exchange_override


def _load_from_ctx(ctx: click.Context) -> AppConfig:
    return _load(ctx.obj["config_path"], ctx.obj.get("exchange_override"))


@main.command()
def init() -> None:
    """Set up a local .env and data directory from scratch."""
    Path("data").mkdir(parents=True, exist_ok=True)
    env_path = Path(".env")
    if not env_path.exists():
        shutil.copyfile(".env.example", env_path)
        console.print("[green]Created .env from .env.example.[/green] Fill in your keys before trading.")
    else:
        console.print(".env already exists, leaving it alone.")
    console.print("Next steps:")
    console.print("  1. Edit .env: set ANTHROPIC_API_KEY (needed either way).")
    console.print(
        "  2. Pick a venue in config/default.yaml (`exchange: polymarket` or `kalshi`), "
        "or pass --exchange on any command."
    )
    console.print("  3. Run: [bold]polybot scan[/bold] to preview Claude's analysis with no risk.")


@main.command()
@click.option("--limit", default=None, type=int, help="Override market_scan.max_markets_per_cycle for this run.")
@click.pass_context
def scan(ctx: click.Context, limit: Optional[int]) -> None:
    """Preview: scan markets and print Claude's analysis for each. Never
    places or simulates any order, and does not touch the database."""
    import requests

    from .ai.analyst import ClaudeAnalyst
    from .engine.scanner import MarketScanner
    from .exchanges import build_exchange

    cfg = _load_from_ctx(ctx)
    if limit:
        cfg.market_scan.max_markets_per_cycle = limit

    exchange = build_exchange(cfg)
    scanner = MarketScanner(exchange, cfg.market_scan)
    analyst = ClaudeAnalyst(cfg.anthropic_api_key, cfg.ai)

    try:
        markets = scanner.scan()
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Could not reach {cfg.exchange}'s market data API: {e}[/red]")
        raise SystemExit(1)

    if not markets:
        console.print(f"[yellow]No {cfg.exchange} markets passed the configured filters.[/yellow]")
        return

    table = Table(title=f"Claude analysis on {cfg.exchange} -- {len(markets)} market(s)")
    for col in ("Market", "Price(Y/N)", "Action", "P(YES)", "Conf.", "Edge", "Reasoning"):
        table.add_column(col, overflow="fold")

    for m in markets:
        try:
            d = analyst.analyze(m)
        except Exception as e:  # noqa: BLE001
            table.add_row(m.question[:60], f"{m.yes_price:.2f}/{m.no_price:.2f}", "[red]ERROR[/red]", "-", "-", "-", str(e)[:80])
            continue
        edge = d.fair_value_probability - m.yes_price
        table.add_row(
            m.question[:60],
            f"{m.yes_price:.2f}/{m.no_price:.2f}",
            d.action.value,
            f"{d.fair_value_probability:.2f}",
            f"{d.confidence:.2f}",
            f"{edge:+.2f}",
            d.reasoning[:120],
        )
    console.print(table)


@main.command()
@click.option("--once", is_flag=True, help="Run a single cycle and exit instead of looping.")
@click.pass_context
def run(ctx: click.Context, once: bool) -> None:
    """Run the trading engine: reviews open positions, scans for new
    opportunities, and executes (or simulates, in dry_run mode) trades."""
    from .engine.trader import TradingEngine

    cfg = _load_from_ctx(ctx)
    _guard_live(cfg)

    if cfg.is_live:
        console.print(f"[bold red]LIVE TRADING ENABLED on {cfg.exchange}.[/bold red] Real orders will be placed with real funds.")
    else:
        console.print(f"[bold cyan]Running against {cfg.exchange} in DRY RUN (paper trading) mode.[/bold cyan] No real orders will be placed.")

    engine = TradingEngine(cfg)

    stop = {"flag": False}

    def _handle_sigint(signum, frame):  # noqa: ANN001
        console.print("\n[yellow]Shutdown requested, finishing current cycle...[/yellow]")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    while True:
        try:
            engine.run_cycle()
        except Exception:
            logger.exception("Unhandled error during trading cycle -- continuing to next cycle")

        if once or stop["flag"]:
            break
        console.print(f"Sleeping {cfg.polling_interval_seconds}s until next cycle... (Ctrl+C to stop)")
        for _ in range(cfg.polling_interval_seconds):
            if stop["flag"]:
                break
            time.sleep(1)

    console.print("Stopped.")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current exchange, mode, bankroll, open positions, and P&L."""
    from .exchanges import build_exchange
    from .storage.db import Database

    cfg = _load_from_ctx(ctx)
    db = Database(cfg.logging.db_path)
    exchange = build_exchange(cfg)

    console.print(f"Exchange: [bold]{cfg.exchange}[/bold]   Mode: [bold]{cfg.mode}[/bold]")

    bankroll_note = ""
    if cfg.risk.bankroll_usd > 0:
        bankroll = cfg.risk.bankroll_usd
        bankroll_note = " (fixed in config)"
    elif not exchange.read_only and cfg.is_live:
        bankroll = exchange.get_balance_usd() or 0.0
        bankroll_note = " (live balance)"
    else:
        bankroll = 1000.0
        bankroll_note = " (nominal paper bankroll -- set risk.bankroll_usd or go live to use a real figure)"
    console.print(f"Bankroll: ${bankroll:,.2f}{bankroll_note}")

    stats = db.get_today_stats(venue=cfg.exchange)
    console.print(f"Today: {stats['trades_count']} trade(s), ${stats['realized_pnl']:+,.2f} realized P&L")
    console.print(f"All-time realized P&L on {cfg.exchange}: ${db.get_all_time_realized_pnl(venue=cfg.exchange):+,.2f}")

    positions = db.get_open_positions(venue=cfg.exchange)
    console.print(f"Open positions: {len(positions)}")
    if positions:
        table = Table()
        for col in ("Question", "Outcome", "Shares", "Avg Cost", "Cost Basis"):
            table.add_column(col)
        for p in positions:
            table.add_row(p.question[:60], p.outcome, f"{p.shares:.2f}", f"{p.avg_cost:.4f}", f"${p.shares * p.avg_cost:,.2f}")
        console.print(table)


@main.command()
@click.pass_context
def positions(ctx: click.Context) -> None:
    """List open positions (alias for the position table in `status`)."""
    ctx.invoke(status)


@main.command()
@click.argument("condition_id")
@click.pass_context
def close(ctx: click.Context, condition_id: str) -> None:
    """Manually close an open position at the current market price."""
    from .engine.exit_manager import ExitManager
    from .exchanges import build_exchange
    from .risk.manager import RiskManager
    from .storage.db import Database

    cfg = _load_from_ctx(ctx)
    db = Database(cfg.logging.db_path)
    open_positions = {p.condition_id: p for p in db.get_open_positions(venue=cfg.exchange)}
    pos = open_positions.get(condition_id)
    if pos is None:
        console.print(f"[red]No open {cfg.exchange} position found for condition_id={condition_id}[/red]")
        raise SystemExit(1)

    _guard_live(cfg)
    exchange = build_exchange(cfg)

    exit_mgr = ExitManager(RiskManager(cfg.risk, cfg.ai), db, exchange, dry_run=not cfg.is_live)
    price = exit_mgr.get_current_price(pos) or pos.avg_cost
    exit_mgr.close_position(pos, price, reason="manual_close")
    console.print(f"Closed {pos.question!r} at {price:.4f} (dry_run={not cfg.is_live}).")


@main.command(name="inspect-market")
@click.argument("ref")
@click.pass_context
def inspect_market(ctx: click.Context, ref: str) -> None:
    """Dump the raw market payload for a slug (Polymarket) or ticker
    (Kalshi). Useful for debugging if a venue's API schema changes and
    filtering starts behaving unexpectedly."""
    import json

    import requests

    cfg = _load_from_ctx(ctx)

    try:
        if cfg.exchange == "polymarket":
            from .clob.gamma import GammaClient

            gamma = GammaClient(cfg.polymarket.gamma_host)
            market = gamma.get_market_by_slug(ref)
        else:
            from .exchanges.kalshi import API_PREFIX, KalshiHttpClient

            host = cfg.kalshi.demo_host if cfg.kalshi.use_demo else cfg.kalshi.api_host
            http = KalshiHttpClient(host, cfg.kalshi_api_key_id, cfg.kalshi_private_key_pem)
            data = http.get(f"{API_PREFIX}/markets/{ref}")
            market = data.get("market", data) if isinstance(data, dict) else data
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Could not reach {cfg.exchange}'s market data API: {e}[/red]")
        raise SystemExit(1)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    console.print_json(json.dumps(market, default=str))


@main.command()
@click.pass_context
def approve(ctx: click.Context) -> None:
    """One-time on-chain approvals so your wallet can trade on Polymarket's
    CTF Exchange (EOA wallets only -- email/Magic and Safe wallets get
    gasless allowances automatically). Polymarket-only; Kalshi is a regular
    brokerage-style account with no on-chain approvals needed."""
    cfg = _load_from_ctx(ctx)
    if cfg.exchange != "polymarket":
        console.print(f"[red]`polybot approve` is Polymarket-only (current exchange: {cfg.exchange}).[/red]")
        raise SystemExit(1)
    if not cfg.private_key:
        console.print("[red]POLYMARKET_PRIVATE_KEY is not set.[/red]")
        raise SystemExit(1)

    from scripts.setup_allowances import run_allowance_setup

    console.print(
        "[yellow]This will send on-chain approval transactions on Polygon and requires POL for gas.[/yellow]"
    )
    click.confirm("Continue?", abort=True)
    run_allowance_setup(cfg.private_key, cfg.polymarket.chain_id)


if __name__ == "__main__":
    sys.exit(main())
