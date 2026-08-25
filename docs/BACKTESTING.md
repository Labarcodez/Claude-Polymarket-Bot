# Backtesting

`polybot backtest` replays historical market data through the **real**
scanner-filter (`passes_filters`) and risk-management (`RiskManager`) code —
only the analysis step (what decides BUY_YES/BUY_NO/HOLD/NO_TRADE) is
swapped for a pluggable strategy. Everything else — sizing, exposure caps,
daily loss limits, stop-loss/take-profit exits — is the exact same code path
a live run uses, replayed against historical dates via an `as_of` parameter
threaded through the ledger (`polybot/storage/db.py`) instead of the real
wall clock. This is deliberate: a parallel reimplementation of the risk
logic for backtesting purposes would risk quietly drifting from what's
actually deployed.

## Quick start (no real data required)

```bash
polybot backtest --demo --strategy null      # sanity check: must show 0 trades, $0 P&L
polybot backtest --demo --strategy random    # noise-trader baseline
polybot backtest --demo --strategy claude    # the real analyst -- costs real API calls
```

`--demo` generates a **synthetic, calibrated-by-construction** dataset: each
market's price follows a driftless random walk, and its resolution is
sampled with probability equal to its own final price. This makes it a
**null-hypothesis engine sanity check**, not a demo of profitability — over
a large enough sample, no strategy should show a *consistent* positive
return against data built this way (small-sample runs will show noisy
positive and negative results either way; that's expected, not a bug — see
"Interpreting results" below). It exists to prove the mechanics work, not to
tell you anything about real trading edge.

## Running against real historical data

Provide two CSVs:

```bash
polybot backtest --snapshots history.csv --resolutions resolutions.csv --strategy claude
```

**`snapshots.csv`** — a time series of market states. One row per
(market, timestamp). Required: `condition_id`, `ts` (ISO 8601), `yes_price`.
Optional: `venue`, `question`, `slug`, `yes_token_id`, `no_token_id`,
`no_price`, `best_bid_yes`, `best_ask_yes`, `spread`, `volume_24hr`,
`liquidity`, `end_date`, `tags` (semicolon-separated).

**`resolutions.csv`** — one row per market, once: `condition_id`, `outcome`
(`YES`/`NO`), `resolved_ts`.

### Fetching real data

This project's own sandbox cannot reach either venue's API directly (see
the note on network access elsewhere in this repo), so these commands are
for you to run from a normal internet connection — they were not run by
Claude to produce any bundled dataset. Verify current parameters against
the venue before relying on the output, and see `docs/RESEARCH_NOTES.md`
for the full research (including confidence caveats) behind these:

**Polymarket** — `GET https://clob.polymarket.com/prices-history` is public
(no auth), taking `market` (the CLOB token/asset ID), a time range
(`startTs`/`endTs` or `interval`), and `fidelity` (minutes). Known
limitation: for already-resolved markets, some reports describe the API
falling back to coarse (12h+) granularity even on markets with clear
intraday movement ([py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216)) —
verify granularity on the specific markets you care about. You'll need a
separate call (e.g. `GET https://gamma-api.polymarket.com/markets?condition_id=...`)
to get each market's actual resolution and `question`/`endDate`.

**Kalshi** — candlesticks live at a `/markets/{ticker}/candlesticks`-style
endpoint (older data may live under a separate `/historical/` namespace);
period length is one of `{1, 60, 1440}` minutes. Public market-data reads
are generally documented as not requiring auth, though this project has not
independently confirmed that for every endpoint — if a plain GET is
rejected, you'll need the same RSA-PSS signed-request auth
`polybot/exchanges/kalshi.py` implements. `GET /portfolio/settlements` or
the market object's own `result` field gives you resolutions.

**Pre-built datasets** (higher-leverage than scraping day-by-day, if the
scale fits your use case):
- [Dune's curated Polymarket + Kalshi tables](https://docs.dune.com/data-catalog/curated/prediction-markets/overview) —
  free tables cover trades/market details/hourly prices; per-fill Kalshi
  trades and Polymarket parlay/position data are Enterprise-gated.
- [Polymarket-v1 (HuggingFace)](https://huggingface.co/datasets/TimeSeventeen/Polymarket-v1) —
  1.2B trades, Nov 2022–Apr 2026, parquet, with on-chain-verified (not
  heuristic) trade direction — the largest/most rigorously documented dump
  found during research.
- [manja316/polymarket-historical-data](https://github.com/manja316/polymarket-historical-data),
  [SII-WANGZJ/Polymarket_data](https://github.com/SII-WANGZJ/Polymarket_data) —
  further large third-party Polymarket dumps.
- [DanMcInerney/kalshi-analysis](https://github.com/DanMcInerney/kalshi-analysis) —
  scrapes Kalshi events/markets/candlesticks/fills into a local DuckDB.
- [Polymarket's public subgraph](https://github.com/Polymarket/polymarket-subgraph) (The Graph)
  or raw `OrderFilled` events via `eth_getLogs` against a Polygon RPC, for
  guaranteed-complete on-chain ground truth.

None of these were verified by fetching data through them in this project —
confirm shape and freshness yourself before trusting a backtest built on one.

## The look-ahead trap specific to LLM strategies

This is the single most important thing to get right, and it's easy to
miss: **`--strategy claude` against a resolved historical market is not
purely a query about the future.** If the event is one Claude may have
encountered during training (a past election, a well-known sports result, a
famous market move), its "analysis" can be recalling the answer rather than
forecasting it — and a backtest built this way will look deceptively,
unrealistically good. A rule-based or `random` strategy doesn't have this
problem; `claude` does, structurally.

Mitigations, roughly in order of rigor:
- Prefer markets that resolved recently relative to the model's training
  cutoff, or markets on genuinely obscure/low-salience questions unlikely
  to appear in training data.
- Treat any backtest result from `--strategy claude` on older or famous
  events as an upper bound on realistic performance, not an estimate of it.
- The only fully clean test is genuinely forward-looking: run `polybot
  scan`/`polybot run` in `dry_run` on markets that haven't resolved yet and
  track results going forward. Nothing about a backtest, however careful,
  substitutes for that.

## Interpreting results

`polybot backtest` reports, among other fields:

- **Brier score** (strategy vs. market-implied) — the real test of whether
  a strategy is adding information beyond what the market price already
  reflected, not just how a particular sample of trades happened to turn
  out. Lower is better; compare the strategy's score to the market's own
  (using the market price at decision time as its "prediction") on the
  *same* set of resolved markets.
- **Return / drawdown / win rate** — from the same risk-managed position
  sizing a live run would use, replayed against historical prices.

A single run — especially against a small sample, and especially with a
strategy involving genuine randomness or an LLM's model-to-model variance —
tells you very little by itself. When this project's own author stress-tested
the `random` (noise-trader) baseline across 10 different seeds and a larger
synthetic sample, individual runs ranged from **-14.9% to +36.5%** with a
mean of +3.9% and a standard deviation of over 15 percentage points — i.e.
consistent with pure noise around zero, not a real edge, despite individual
runs looking dramatic in isolation. Don't trust one backtest run, and don't
trust a strategy just because one run of it made money. See
`docs/RESEARCH_NOTES.md` for what actually-rigorous published research
found when frontier LLMs traded these platforms with real capital.

## What this does *not* model

- **Fees.** Neither Kalshi's per-contract fee formula nor Polymarket's
  dynamic taker fee are charged in the simulation. Real returns will be
  lower than a backtest shows, particularly on Kalshi where fees peak
  around 50¢ (see `docs/RESEARCH_NOTES.md`).
- **Partial fills / rejected orders.** Every entry and exit is assumed to
  fill completely at the computed limit price (the same assumption
  `dry_run` uses live). Real liquidity is frequently much thinner than a
  snapshot's `liquidity` field implies, especially away from the money.
- **Regime change.** A strategy validated on one window of history has no
  guarantee of holding up going forward -- prediction markets are
  young, fast-evolving products (fee schedules, bot competition, and
  liquidity have all changed materially within any of the historical
  datasets referenced above).
