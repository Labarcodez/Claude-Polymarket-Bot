# Claude-Polymarket-Bot

An automated trading bot for [Polymarket](https://polymarket.com) that uses
[Claude](https://www.anthropic.com/claude) to analyze prediction markets and
a deterministic risk engine to size and gate every trade.

**⚠️ Read [docs/RISK_DISCLAIMER.md](docs/RISK_DISCLAIMER.md) before setting
`mode: live`. There is no guarantee this bot makes money — it can lose
money, including all funds in any wallet you connect to it. It defaults to
paper trading (`mode: dry_run`) and stays there until you deliberately flip
two independent safety switches.**

## How it works

Every cycle, the bot:

1. **Reviews open positions** against rule-based stop-loss / take-profit /
   near-resolution exits (no AI call needed for this).
2. **Scans** Polymarket's Gamma API for active, liquid, binary markets and
   filters out illiquid, near-certain, or soon-to-resolve ones.
3. **Asks Claude** to analyze each surviving market: a calibrated probability
   estimate, a confidence score, reasoning, and risk flags — returned as a
   structured tool call, not free text.
4. **Risk-sizes** any decision that clears a confidence/edge bar using
   fractional-Kelly sizing, clipped by hard per-trade, per-market, total
   exposure, and daily-loss/trade-count limits that Claude's output can
   never override.
5. **Executes** (or, in `dry_run`, simulates) the resulting order and logs
   everything — decisions, orders, positions, P&L — to a local SQLite
   database.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full data flow and
design rationale.

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- A Polygon wallet funded with USDC.e, **only** if/when you go live (not
  needed for `dry_run`)

## Setup

```bash
git clone <this-repo>
cd Claude-Polymarket-Bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

polybot init          # creates .env from .env.example and data/
```

Edit `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

That's all you need for paper trading. Try it immediately:

```bash
polybot scan           # preview: Claude's analysis on live markets, no orders, no DB writes
polybot run --once     # one full cycle: scan, decide, risk-size, simulate, log to DB
polybot status          # bankroll, open (paper) positions, P&L so far
polybot run              # loop forever on config.polling_interval_seconds (Ctrl+C to stop)
```

Tune `config/default.yaml` — market filters, Claude's model/effort, and
every risk parameter — to taste. It's heavily commented.

## Going live (real money)

Only after you've watched `dry_run` behave sensibly for a while:

1. **Wallet setup.** Create a Polygon wallet dedicated to this bot only —
   never reuse a wallet holding other funds. Fund it with USDC.e (and a
   little POL if it's a plain MetaMask-style wallet, for gas). In `.env`:
   ```bash
   POLYMARKET_PRIVATE_KEY=0x...
   # Only if trading via a Polymarket email/Magic login or a Gnosis Safe /
   # proxy wallet -- see polymarket.com -> Settings for this address, and
   # set polymarket.signature_type accordingly (1 or 2) in config:
   POLYMARKET_FUNDER_ADDRESS=0x...
   ```
2. **One-time on-chain approvals** (EOA / `signature_type: 0` wallets only —
   email/Magic and Safe wallets get these gaslessly and can skip this):
   ```bash
   polybot approve
   ```
3. **Flip both safety gates.** In `config/default.yaml`: `mode: live`. In
   `.env`: `POLYBOT_CONFIRM_LIVE=yes`. These are separate on purpose — see
   `_guard_live` in `polybot/cli.py`.
4. Start small: keep `risk.bankroll_usd` and `risk.max_position_usd` low
   until you trust the sizing and the model's judgment on real capital.

```bash
polybot run
```

## CLI reference

| Command | Effect |
|---|---|
| `polybot init` | Create `.env` and `data/` |
| `polybot scan [--limit N]` | Preview Claude's analysis on current markets; no orders, no DB writes |
| `polybot run [--once]` | Run the full engine, looping by default |
| `polybot status` / `polybot positions` | Bankroll, open positions, P&L |
| `polybot close CONDITION_ID` | Manually close one position now |
| `polybot approve` | One-time on-chain token allowances (EOA wallets) |
| `polybot inspect-market SLUG` | Dump a market's raw Gamma API payload (debugging) |

All commands accept `--config path/to/file.yaml` to use an alternate config.

## Project layout

```
polybot/
  cli.py               CLI entry point
  config.py             YAML + env config loading
  clob/
    gamma.py             Gamma API (public market/event discovery)
    data_api.py           Data API (public positions/trades)
    client.py              py-clob-client wrapper (reads + order execution)
  ai/
    prompts.py             Claude system/user prompts
    analyst.py               ClaudeAnalyst: market -> TradeDecision
  risk/
    manager.py                Kelly sizing, exposure/daily limits, exit rules
  engine/
    models.py                  Shared pydantic models
    scanner.py                  Gamma -> filtered MarketSnapshot list
    trader.py                    TradingEngine: orchestrates one cycle
    exit_manager.py               Rule-based position exits
  storage/
    db.py                          SQLite ledger
scripts/
  setup_allowances.py               On-chain USDC/CTF allowance setup
tests/                                pytest suite for the pure logic
docs/
  ARCHITECTURE.md
  RISK_DISCLAIMER.md
```

## Testing

```bash
pytest -q
```

The suite covers the parts of the system that don't require live network
access or a funded wallet: Kelly sizing math, market filtering, risk
gating/exposure limits, and the SQLite ledger. It does **not** exercise
`ClaudeAnalyst` (a live API call) or `PolyTradingClient` (a live, signed
trade) end-to-end — `polybot scan` and `polybot run --once` in `dry_run` are
the closest thing to an integration test, against real Polymarket data,
without ever risking funds.

## A note on Polymarket's API surface

This bot is built on the official `py-clob-client` Python SDK and the public
Gamma/Data APIs. `py-clob-client` is Polymarket's original client and, as of
this writing, is archived in favor of a newer unified `py-sdk`; it still
works against the live CLOB API and is what this project targets for
stability and documentation maturity. If Polymarket removes it entirely,
`polybot/clob/client.py` is the only file that needs to change to target a
successor SDK — everything else in this project talks to `PolyTradingClient`
through a small, stable interface.

The Gamma API's JSON schema also isn't formally versioned. If market
filtering in `polybot scan` starts silently returning nothing, run
`polybot inspect-market <slug>` on a market you know should qualify and
check the field names in `engine/scanner.py::parse_market` against the raw
payload.

## License

MIT — see [LICENSE](LICENSE).
