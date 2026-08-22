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
 │  Gamma API -> filter │  │   manager.py    │   │  SQLite: decisions,  │
 │  -> MarketSnapshot[] │  │  rule-based     │   │  orders, positions,  │
 └─────────┬───────────┘  │  stop/TP/time   │   │  daily_stats         │
           │              └────────┬────────┘   └──────────▲──────────┘
           ▼                       │                        │
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
 ┌───────────────────┐                                          │
 │  PolyTradingClient  │───────── live orders / balances ────────┘
 │   clob/client.py     │         (skipped entirely in dry_run)
 │  wraps py-clob-client │
 └───────────────────┘
```

## Data flow of one cycle (`TradingEngine.run_cycle`)

1. **Exit review** (`ExitManager.review_all`) -- for every open position in
   the DB, fetch the current midpoint, and close it if it has hit take
   profit, stop loss, or is close enough to resolution to force an exit.
   Pure rule-based; does not call Claude.
2. **Portfolio snapshot** -- bankroll (configured, or read from the live
   wallet, or a nominal paper figure), open positions, today's trade count
   and realized P&L.
3. **Daily circuit breakers** -- if the daily loss or trade-count cap is
   already hit, or the open-position cap is reached, skip straight to the
   end of the cycle (exits above still ran).
4. **Scan** (`MarketScanner.scan`) -- pull a pool of active markets from the
   Gamma API, parse them into `MarketSnapshot`s, and apply the configured
   filters (volume, liquidity, spread, price range, time-to-resolution,
   tags). This step is deliberately conservative: most of Polymarket's
   markets are illiquid, near-certain, or about to resolve, and are filtered
   out before ever reaching Claude.
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
   "safety" of this bot lives here, not in the prompt.
7. **Execute** -- in `dry_run` (default), the order is simulated and written
   straight to the local ledger as an assumed fill. In `live`, a FOK
   (fill-or-kill) market order is submitted through `py-clob-client`; FOK is
   used deliberately so a non-error response means the trade actually
   filled, keeping the ledger's "assume filled" bookkeeping accurate instead
   of guessing at a resting order's eventual fill.

## Why FOK instead of resting GTC orders for entries/exits

An earlier design used resting `GTC` limit orders for entries. The problem:
the bot books a position into its ledger as soon as it submits an order, but
a GTC order might sit unfilled (or partially filled) for an arbitrary amount
of time. `FOK` (fill-or-kill) collapses that ambiguity: either the whole
order fills right now (safe to book), or it's rejected and nothing is
booked. The tradeoff is that some marginal, edge-of-book trades will fail to
fill rather than resting and waiting -- an acceptable cost given how much it
simplifies (and de-risks) the ledger's correctness.

## Why exits don't call Claude

Take-profit, stop-loss, and near-resolution exits are enforced purely by
`RiskManager.evaluate_exit` against static thresholds in config. There's no
upside to spending an API call asking "should I still hold this?" when the
answer is "you're past your stop loss, close it" -- and a design that lets
the model talk itself out of a stop loss defeats the point of having one.

## Extending this project

- **Smarter scanning**: incorporate `/events` grouping, multi-outcome
  (negative-risk) markets, or a news/context tool call for the analyst.
- **Position-aware re-analysis**: today `HOLD` is a no-op; you could route
  open positions back through `ClaudeAnalyst` periodically (separately from
  the hard stop-loss/take-profit rules) to catch a thesis that has
  genuinely changed.
- **Backtesting**: `engine/scanner.py`'s `parse_market`/`passes_filters` and
  `risk/manager.py` are pure functions decoupled from any live client,
  which makes them straightforward to replay against historical Gamma/CLOB
  data if you build a fetcher for it.
- **Observability**: the SQLite ledger is intentionally simple; point a BI
  tool or a small dashboard at it, or swap in Postgres by changing only
  `storage/db.py`.
