# Claude-Polymarket-Bot

An automated trading bot for [Polymarket](https://polymarket.com) and
[Kalshi](https://kalshi.com) prediction markets that uses
[Claude](https://www.anthropic.com/claude) to analyze markets and a
deterministic risk engine to size and gate every trade. Pick a venue with
one config field (`exchange: polymarket` or `exchange: kalshi`) — everything
else (scanning, analysis, sizing, exits, the ledger) works the same either way.

**⚠️ Read [docs/RISK_DISCLAIMER.md](docs/RISK_DISCLAIMER.md) before setting
`mode: live`. There is no guarantee this bot makes money — it can lose
money, including all funds in any account you connect to it. It defaults to
paper trading (`mode: dry_run`) and stays there until you deliberately flip
two independent safety switches.**

## How it works

Every cycle, the bot:

1. **Reviews open positions** against rule-based stop-loss / take-profit /
   near-resolution exits (no AI call needed for this).
2. **Scans** the active venue for active, liquid, binary markets and filters
   out illiquid, near-certain, or soon-to-resolve ones.
3. **Asks Claude** to analyze each surviving market: a calibrated probability
   estimate, a confidence score, reasoning, and risk flags — returned as a
   structured tool call, not free text.
4. **Risk-sizes** any decision that clears a confidence/edge bar using
   fractional-Kelly sizing, clipped by hard per-trade, per-market, total
   exposure, and daily-loss/trade-count limits that Claude's output can
   never override.
5. **Executes** (or, in `dry_run`, simulates) the resulting order and logs
   everything — decisions, orders, positions, P&L — to a local SQLite
   database, tagged by which venue produced it.

Steps 1, 3, and 4 are entirely venue-agnostic. Only "how do I fetch/parse
markets" and "how do I place an order" differ per venue, and that logic is
isolated behind one interface (`polybot/exchanges/base.py`) — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full data flow and
design rationale.

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- Depending on which venue you trade (not needed for `dry_run` on either):
  - **Polymarket**: a Polygon wallet funded with USDC.e
  - **Kalshi**: a Kalshi account and an API key (RSA key pair)

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
polybot scan                       # preview: Claude's analysis on live markets, no orders, no DB writes
polybot run --once                 # one full cycle: scan, decide, risk-size, simulate, log to DB
polybot status                     # bankroll, open (paper) positions, P&L so far
polybot run                        # loop forever on config.polling_interval_seconds (Ctrl+C to stop)

polybot --exchange kalshi scan     # same thing, against Kalshi instead -- or set exchange: kalshi in config
```

Tune `config/default.yaml` — which venue, market filters, Claude's
model/effort, and every risk parameter — to taste. It's heavily commented.
`--exchange polymarket|kalshi` on any command overrides `config.exchange`
for that invocation without editing the file (as does `POLYBOT_EXCHANGE` in
`.env`).

## Going live (real money)

Only after you've watched `dry_run` behave sensibly for a while, **and**
read [docs/RISK_DISCLAIMER.md](docs/RISK_DISCLAIMER.md).

### Polymarket

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

### Kalshi

1. **Account + API key.** Sign up at kalshi.com, then go to Settings -> API
   Keys and generate a key pair. Kalshi shows you the Key ID and lets you
   download the private key once — save it. In `.env`:
   ```bash
   KALSHI_API_KEY_ID=...
   KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
   ```
2. No on-chain approvals needed — Kalshi is a regular (CFTC-regulated,
   US-facing) brokerage-style account funded via ACH/debit, not a crypto wallet.
3. Consider trying `kalshi.use_demo: true` in config first — Kalshi runs a
   free paper-trading sandbox with its own separate account/API keys.

### Then, for either venue

1. **Flip both safety gates.** In `config/default.yaml`: `mode: live`. In
   `.env`: `POLYBOT_CONFIRM_LIVE=yes`. These are separate on purpose — see
   `_guard_live` in `polybot/cli.py`.
2. Start small: keep `risk.bankroll_usd` and `risk.max_position_usd` low
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
| `polybot status` / `polybot positions` | Bankroll, open positions, P&L (for the active exchange) |
| `polybot decisions [--limit N]` | Review Claude's most recent analyses, traded or not |
| `polybot reconcile` | Diff the local ledger against the venue's own live positions (requires credentials, places no orders) |
| `polybot close CONDITION_ID` | Manually close one position now |
| `polybot approve` | One-time on-chain token allowances (Polymarket EOA wallets only) |
| `polybot inspect-market REF` | Dump a market's raw API payload -- a slug (Polymarket) or ticker (Kalshi) |

All commands accept `--config path/to/file.yaml` and `--exchange polymarket|kalshi`.

## Project layout

```
polybot/
  cli.py                 CLI entry point
  config.py               YAML + env config loading (both venues)
  exchanges/
    base.py                 ExchangeAdapter interface every venue implements
    polymarket.py             Polymarket adapter (Gamma discovery + py-clob-client execution)
    kalshi.py                  Kalshi adapter (REST + RSA-PSS signed requests)
  clob/
    gamma.py                    Gamma API (public market/event discovery)
    data_api.py                  Data API (public positions/trades)
    client.py                     py-clob-client wrapper (reads + order execution)
  ai/
    prompts.py                     Claude system/user prompts
    analyst.py                      ClaudeAnalyst: market -> TradeDecision
  risk/
    manager.py                       Kelly sizing, exposure/daily limits, exit rules
  engine/
    models.py                         Shared, venue-agnostic pydantic models
    scanner.py                         Venue-agnostic market filtering
    trader.py                           TradingEngine: orchestrates one cycle
    exit_manager.py                      Rule-based position exits
  storage/
    db.py                                  SQLite ledger, tagged per venue
scripts/
  setup_allowances.py                       On-chain USDC/CTF allowance setup (Polymarket only)
tests/                                        pytest suite for the pure logic
docs/
  ARCHITECTURE.md
  RISK_DISCLAIMER.md
```

## Testing

```bash
pytest -q
```

The suite covers the parts of the system that don't require live network
access or real credentials: Kelly sizing math, market filtering, risk
gating/exposure limits, the SQLite ledger (including its cross-venue
migration path), the Claude API call shape (mocked), Polymarket/Kalshi
market parsing, and Kalshi's RSA-PSS request signing (verified against a
throwaway keypair). It does **not** exercise a live API call or a live,
signed trade end-to-end on either venue — `polybot scan` and
`polybot run --once` in `dry_run` are the closest thing to an integration
test, against real market data, without ever risking funds.

## A note on both venues' API surfaces

This bot is built on Polymarket's official `py-clob-client` Python SDK plus
the public Gamma/Data APIs, and a small hand-rolled client for Kalshi's REST
API (there's no dependency-free official Kalshi Python SDK for the RSA-PSS
signing scheme it uses). Both venues' JSON schemas are effectively
undocumented-in-the-strict-sense and can drift:

- **Polymarket**: if market filtering in `polybot scan` starts silently
  returning nothing, run `polybot inspect-market <slug>` on a market you
  know should qualify and check the field names in
  `exchanges/polymarket.py::parse_market` against the raw payload.
  `py-clob-client` itself is archived (Polymarket points new projects at a
  successor `py-sdk`) but still works against the live CLOB API as of this
  writing; if that changes, `polybot/clob/client.py` is the only file that
  needs to change to target a replacement.
- **Kalshi**: this project was built from Kalshi's documented request/response
  field names (verified against multiple independent sources) but has **not**
  been run against a live, funded Kalshi account by its authors -- in
  particular, the exact shape of an order-creation response (used to
  reconcile how many contracts actually filled) is unverified. See the
  loud warning `KalshiExchange._reconcile_fill` logs if it can't recognize
  the response, and `docs/RISK_DISCLAIMER.md`. Run `polybot inspect-market
  <ticker>` to check the raw market payload if `polybot scan` looks wrong.

## License

MIT — see [LICENSE](LICENSE).
