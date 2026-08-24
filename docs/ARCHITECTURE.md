# Architecture

```
                        ┌─────────────────────┐
                        │   polybot run/scan   │  (CLI, click)
                        └──────────┬───────────┘
                                   │
                       ┌───────────▼────────────┐
                       │     TradingEngine        │  engine/trader.py
                       │  orchestrates one cycle  │
                       └─┬───────┬───────┬───────┘
                         │       │       │
           ┌─────────────┘       │       └─────────────┐
           ▼                     ▼                      ▼
 ┌───────────────────┐  ┌────────────────┐   ┌────────────────────┐
 │   MarketScanner     │  │  ExitManager    │   │     Database         │
 │  engine/scanner.py  │  │ engine/exit_    │   │  storage/db.py       │
 │  fetch -> filter     │  │   manager.py    │   │  SQLite: decisions,  │
 │  -> MarketSnapshot[] │  │  rule-based     │   │  orders, positions,  │
 └─────────┬───────────┘  │  stop/TP/time   │   │  daily_stats,         │
           │              └────────┬────────┘   │  all tagged by venue  │
           ▼                       │             └──────────▲──────────┘
 ┌───────────────────┐             │                        │
 │   ClaudeAnalyst     │            │                        │
 │   ai/analyst.py     │            │                        │
 │  1 market -> 1       │            │                        │
 │  TradeDecision via   │            │                        │
 │  forced tool call    │            │                        │
 └─────────┬───────────┘             │                        │
           ▼                        │                        │
 ┌───────────────────┐              │                        │
 │    RiskManager      │◄────────────┘                        │
 │   risk/manager.py    │                                      │
 │  TradeDecision +      │                                      │
 │  PortfolioState        │                                     │
 │  -> OrderPlan | None    │                                    │
 │  (sizing, exposure,     │                                    │
 │   daily limits)          │                                   │
 └─────────┬───────────────┘                                   │
           ▼                                                    │
 ┌────────────────────────┐                                     │
 │      ExchangeAdapter      │──────── live orders / balances ────┘
 │  exchanges/base.py (ABC)   │        (skipped entirely in dry_run)
 │  ┌──────────┐ ┌──────────┐ │
 │  │Polymarket│ │  Kalshi  │ │  one instance, picked by config.exchange
 │  │ adapter  │ │ adapter  │ │  via exchanges.build_exchange()
 │  └──────────┘ └──────────┘ │
 └────────────────────────┘
```

## The exchange abstraction

Everything above `ExchangeAdapter` -- `TradingEngine`, `ExitManager`,
`MarketScanner`, `RiskManager` -- is written entirely against the interface
in `polybot/exchanges/base.py` and has no idea whether it's trading
Polymarket or Kalshi. `exchanges.build_exchange(config)` picks and
constructs the right adapter (`exchanges/polymarket.py` or
`exchanges/kalshi.py`) once, at `TradingEngine` startup, from
`config.exchange`. That's the entire seam: adding a third venue means
implementing five methods on a new `ExchangeAdapter` subclass, not touching
the engine.

`ExchangeAdapter` methods, and who's responsible for what:

- `fetch_raw_markets(pool_size)` / `enrich_candidates(snapshots)` -- venue
  discovery, entirely owned by the adapter (Gamma API pagination and JSON
  shape vs. Kalshi's `/markets` and cents-based pricing are completely
  different, and stay contained there).
- `get_midpoint(market_ref, outcome)` -- current price for one side of one
  market, used by `ExitManager` to mark open positions.
- `get_balance_usd()` -- account balance in dollars.
- `execute_entry(plan)` / `execute_exit(position, price_hint)` -- submit a
  *real* order and return an `ExecutionResult` (success, shares filled,
  actual fill price, order id). Each venue's fill semantics differ
  (Polymarket FOK is all-or-nothing; Kalshi has no FOK order type, so the
  adapter submits an aggressively-priced limit order and reconciles the
  actual fill count from the response) -- that messiness is exactly what
  this method exists to absorb.

Simulated (`dry_run`) fills are handled once, centrally, in
`TradingEngine._execute_entry` and `ExitManager.close_position` -- adapters
only ever need to implement the *live* path, which keeps them focused on
the one thing that's actually venue-specific.

Some field names on the shared models (`MarketSnapshot`, `OrderPlan`,
`OpenPosition` in `engine/models.py`) carry over Polymarket's vocabulary for
historical reasons but are venue-generic in practice -- see the docstring on
`MarketSnapshot`. Notably `condition_id` holds Polymarket's on-chain
conditionId *or* Kalshi's market ticker, and `yes_token_id`/`no_token_id`
hold Polymarket's CLOB token ids *or* (both equal to) the Kalshi ticker,
since Kalshi addresses an order by ticker + side rather than distinct
per-outcome ids.

## Data flow of one cycle (`TradingEngine.run_cycle`)

1. **Exit review** (`ExitManager.review_all`) -- for every open position in
   the DB *for the active venue*, fetch the current midpoint via the
   exchange adapter, and close it if it has hit take profit, stop loss, or
   is close enough to resolution to force an exit. Pure rule-based; does
   not call Claude.
2. **Portfolio snapshot** -- bankroll (configured, or read live via the
   adapter, or a nominal paper figure), the active venue's open positions,
   today's trade count and realized P&L (also venue-scoped).
3. **Daily circuit breakers** -- if the daily loss or trade-count cap is
   already hit, or the open-position cap is reached, skip straight to the
   end of the cycle (exits above still ran).
4. **Scan** (`MarketScanner.scan`) -- ask the adapter for a pool of active
   markets, already parsed into `MarketSnapshot`s, and apply the configured
   filters (volume, liquidity, spread, price range, time-to-resolution,
   tags) via the shared, venue-agnostic `passes_filters`. This step is
   deliberately conservative: most markets on either venue are illiquid,
   near-certain, or about to resolve, and are filtered out before ever
   reaching Claude.
5. **Analyze** (`ClaudeAnalyst.analyze`) -- for each surviving market, one
   Claude call with a forced tool call (`submit_analysis`) returns a
   calibrated `TradeDecision`: probability estimate, confidence, a
   suggested (non-binding) size, reasoning, and risk flags. The system
   prompt is cache-annotated since it's identical across every market in a
   cycle.
6. **Risk-size** (`RiskManager.plan_entry_order`) -- rejects low-confidence
   or low-edge decisions outright, then sizes anything that passes with a
   fractional-Kelly formula, clipped by every configured hard cap (per-trade,
   per-market, total exposure, remaining bankroll). Most of the actual
   "safety" of this bot lives here, not in the prompt, and it never touches
   an exchange client directly.
7. **Execute** -- in `dry_run` (default), the order is simulated centrally
   in `TradingEngine`/`ExitManager` and written straight to the local ledger
   as an assumed fill. In `live`, `ExchangeAdapter.execute_entry` /
   `execute_exit` submits a real order and the ledger books whatever
   `ExecutionResult` actually reports -- not just what was requested.

## Why FOK (or an aggressive crossing limit) instead of resting orders

An earlier design used resting `GTC` limit orders for entries. The problem:
the bot books a position into its ledger as soon as it submits an order, but
a resting order might sit unfilled (or partially filled) for an arbitrary
amount of time. On Polymarket, `FOK` (fill-or-kill) collapses that
ambiguity: either the whole order fills right now (safe to book), or it's
rejected and nothing is booked. Kalshi's order API has no FOK order type, so
`KalshiExchange` instead submits a limit order priced aggressively enough to
almost certainly cross the book immediately, then reads back the response's
fill-count field to book only what actually filled (see the honesty note in
`KalshiExchange._reconcile_fill` -- that response shape is the one part of
this project unverified against a live account). Either way, the tradeoff is
the same: some marginal, edge-of-book trades fail to fill rather than
resting and waiting, which is an acceptable cost given how much it
simplifies (and de-risks) the ledger's correctness.

## Why exits don't call Claude

Take-profit, stop-loss, and near-resolution exits are enforced purely by
`RiskManager.evaluate_exit` against static thresholds in config. There's no
upside to spending an API call asking "should I still hold this?" when the
answer is "you're past your stop loss, close it" -- and a design that lets
the model talk itself out of a stop loss defeats the point of having one.
This is also entirely venue-agnostic, since it only ever looks at price and
time, never at how the position was opened.

## Extending this project

- **A third venue**: implement `ExchangeAdapter`'s five methods against the
  new venue's API in a new `exchanges/<venue>.py`, add it to
  `exchanges.build_exchange`, and add its secrets/config section to
  `config.py`. Nothing in `engine/` or `risk/` needs to change.
- **Smarter scanning**: incorporate Polymarket `/events` grouping or
  multi-outcome (negative-risk) markets, Kalshi event/series grouping, or a
  news/context tool call for the analyst.
- **Position-aware re-analysis**: today `HOLD` is a no-op; you could route
  open positions back through `ClaudeAnalyst` periodically (separately from
  the hard stop-loss/take-profit rules) to catch a thesis that has
  genuinely changed.
- **Backtesting**: `engine/scanner.py::passes_filters`,
  `exchanges/{polymarket,kalshi}.py::parse_market`, and `risk/manager.py`
  are pure functions decoupled from any live client, which makes them
  straightforward to replay against historical data if you build a fetcher
  for it.
- **Observability**: the SQLite ledger is intentionally simple; point a BI
  tool or a small dashboard at it, or swap in Postgres by changing only
  `storage/db.py`.
