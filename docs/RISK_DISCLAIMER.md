# Risk Disclaimer

Read this before you ever set `mode: live` in `config/default.yaml`, on
either supported venue (Polymarket or Kalshi).

## This is not financial advice, and there is no guarantee of profit

This project connects a large language model (Claude) to a real trading
venue. Nothing about that combination guarantees, implies, or is likely to
produce profit. Prediction markets are close to efficient most of the time;
an LLM's probability estimate is a model output, not ground truth, and it
will sometimes be confidently wrong. Treat every number this bot produces --
probabilities, confidence scores, suggested sizes -- as a fallible opinion,
not a fact.

**You can lose money running this bot, including the entire balance of any
wallet or account you connect to it.** Only connect a wallet/account funded
with money you can afford to lose completely, and never your primary wallet
or one holding unrelated funds/permissions.

## What the risk layer does and does not protect you from

`polybot/risk/manager.py` enforces hard limits: per-trade size caps, per-market
and total exposure caps, a daily loss cap, a daily trade-count cap, and
rule-based stop-loss / take-profit / near-resolution exits. These limit how
fast and how far things can go wrong through the bot's *own* decision loop.

They do **not** protect you from:
- Systematic bias in Claude's market analysis (e.g. consistently
  overconfident in one direction) -- bad calibration can lose money slowly,
  within the limits, for a long time before it's obvious.
- Smart-contract, exchange, custody, or key-management risk on Polymarket's
  side, or brokerage/counterparty risk on Kalshi's side.
- Market manipulation, oracle/resolution disputes, or ambiguous resolution
  criteria on individual markets.
- A bug in this codebase. It has unit tests for the pure logic (sizing,
  filtering, exit rules, both venues' market parsing, Kalshi's request
  signing) but has **not** been run against a live, funded account on
  either venue by its authors. Review the code yourself before trusting it
  with money.
- **Specifically on Kalshi:** the exact response shape of Kalshi's
  order-creation endpoint (used to figure out how many contracts actually
  filled) is unverified against a live account -- see
  `KalshiExchange._reconcile_fill` in `polybot/exchanges/kalshi.py`, which
  logs loudly and falls back to an optimistic "assume it filled" if the
  response doesn't look as expected. Check your actual Kalshi position
  against `polybot status` periodically while live, especially early on.
- Anthropic API, Polymarket API, or Kalshi API outages/latency during a
  fast-moving market.

## Start in dry_run, stay there a while

The default `mode: dry_run` never touches a wallet -- it simulates fills and
tracks a paper P&L in the local SQLite database. Run it for a meaningful
stretch of time (weeks, not minutes) and actually read the reasoning it
produces (`polybot scan`, `polybot status`) before deciding whether its
judgment is something you trust with real funds, and if so, at what size.

Going live requires two independent, deliberate steps (`mode: live` in
config **and** `POLYBOT_CONFIRM_LIVE=yes` in the environment) specifically so
it can never happen by accident.

## Regulatory / eligibility

Both venues restrict who may trade real money on their platform by
jurisdiction and identity, and this can change over time. Polymarket's terms
of service historically excluded US persons from its main offshore product
and has introduced separate CFTC-regulated US offerings with their own
onboarding and contracts; Kalshi is itself a CFTC-regulated US exchange with
its own eligibility rules (e.g. KYC, residency). It is **your**
responsibility, not this code's, to confirm you're eligible to trade on
whichever venue and product you connect this bot to, and to comply with its
terms and the laws that apply to you. This project does not attempt to
enforce or verify eligibility on either venue.

## Costs beyond the trades themselves

- **Anthropic API usage** costs money per market analyzed, independent of
  whether a trade is placed. `ai.effort` and how many markets you scan per
  cycle are the main levers on this cost -- see `config/default.yaml`.
- **Gas (POL)** is required on Polygon for the one-time allowance-approval
  transactions if you're trading Polymarket from a plain EOA wallet
  (`polybot approve`). Kalshi has no equivalent -- it's a regular funded
  brokerage-style account.
- Market spreads and the slippage buffer (`risk.slippage_bps`) are a real,
  ongoing cost of trading, separate from any edge (or lack of it) in the
  underlying probability estimates, on both venues.
- Kalshi also charges its own per-trade fees (varying by market); this
  project does not model or account for them in sizing or P&L, so realized
  P&L reported by `polybot status` for Kalshi positions will be somewhat
  optimistic relative to what actually lands in your account.

By running this software you accept all of the above. If any of it gives you
pause, that's the software working as intended -- stay in `dry_run`.
