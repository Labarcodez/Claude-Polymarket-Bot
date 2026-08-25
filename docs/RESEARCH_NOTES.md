# Research notes: how money is actually made (and lost) on Polymarket and Kalshi

Compiled from ~20 web searches, ~15 academic paper reads, and cross-checking
of promotional "bot guide" content against peer-reviewed/primary sources.
Several domains commonly cited on this topic are bot-selling or SEO content
farms (clawarbs.com, tradingvps.io, botforkalshi.com, predictionhunt.com,
predscope.com, alphascope.app, laikalabs.ai, marketmath.io's promotional
pages) — their claims are flagged explicitly below and weighted below
academic/primary sources wherever the two conflict, which happens often.
`docs.kalshi.com`, `gamma-api.polymarket.com`, `clob.polymarket.com`,
`arxiv.org`, `medium.com`, and `reddit.com` were unreachable via direct
fetch from this project's environment, so those sources are represented via
search snippets and indexed full-text passages rather than direct reads —
noted where it matters. This file is a research record, not investment
advice, and should be read alongside `docs/RISK_DISCLAIMER.md`.

## The headline finding

**[Prediction Arena](https://arxiv.org/abs/2604.07355)** (Zhang et al.) had
six frontier LLMs trade $10,000 of *real capital*, autonomously, on Kalshi
for 57 days (Jan–Mar 2026). **Every single model lost money** — returns
ranged from -16.0% (GLM-4.7, best) to -30.8% (grok-4-1-fast-reasoning,
worst); Claude Opus 4.5 finished at -25.9%. Settlement win rates never
exceeded 52%. A concurrent Polymarket run (same six models, Feb–Mar) lost
far less on average (-1.1% vs. -22.6%), attributed to Polymarket's open,
discovery-based market selection letting models self-select into
better-priced markets rather than trading a fixed curated universe. This is
the closest thing to ground truth this research found on "what happens if
you just let an LLM trade these platforms," and it directly contradicts
viral "AI bot turned $1K into $14K" claims (one such Medium post was found
and is unverified/promotional — single anecdote, undisclosed methodology).

A companion paper, [Foresight Arena](https://arxiv.org/abs/2605.00420),
makes a fair methodological point against over-reading any one such
study: detecting a genuine 2-point LLM edge over market consensus needs
roughly 350 resolved predictions, below what either 57-day trial can
cleanly resolve. The *direction* of the finding (no LLM beat the market;
most lost real money net of costs) is nonetheless consistent across
everything this research turned up.

## Are LLMs well-calibrated forecasters?

No — and more "reasoning" makes it worse, not better.

**[KalshiBench](https://arxiv.org/abs/2512.16030)** (Nel) tested five
frontier models on 300 Kalshi questions resolving *after* each model's
training cutoff:
- All five were systematically **overconfident**. Expected Calibration
  Error ranged 0.120 (Claude Opus 4.5, best) to 0.395 (GPT-5.2-XHigh,
  worst). In the 90-100% self-reported-confidence bin, accuracy was only
  30.8-70.0% across models.
- **Only Claude Opus 4.5 beat the naive base-rate baseline** (Brier Skill
  Score +0.057); every other model scored *worse* than just guessing the
  historical base rate.
- **More reasoning tokens hurt calibration**: GPT-5.2-XHigh used ~26x more
  output tokens than Claude yet had 3x worse ECE — read as reasoning chains
  reinforcing an initial hypothesis rather than genuinely updating on it.
- By category (Claude Opus 4.5, the best model): strong on Social,
  Entertainment, Sports, Elections; **weak on Crypto (36.4% accuracy,
  n=11)**.

**[ForecastBench](https://arxiv.org/abs/2409.19839)** (Karger et al.,
Wharton/FRI), the standard large academic benchmark: superforecasters
scored Brier 0.096, the general public 0.121, the best LLM (Claude 3.5
Sonnet, *given the human crowd's forecast as an input*) 0.122-0.123 — i.e.
frontier LLMs roughly matched an untrained crowd, not experts, and only
with the crowd's own answer fed in as a feature. Without it, the best
model's Brier rose to 0.136. LLMs were specifically weaker on compound
questions needing joint-probability reasoning.

**Practical implication for this project**: `polybot/ai/prompts.py`'s
system prompt has been updated to reflect this — explicitly warning against
treating high stated confidence as reliable, flagging crypto markets as a
specifically weak category, and telling the model that longer reasoning is
not itself evidence of a better answer.

## Where the *real*, evidenced edge is (and how big it actually is)

### Cross-platform / cross-market arbitrage — real, but tiny and fast

- David Krause's SSRN work on basis-risk arbitrage between related
  contracts found mean profit after fees of 1.4-4.9% depending on the
  contract, present on 64-89% of trading days
  ([paper 1](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6905683),
  [paper 2](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6984101)).
  Persistence attributed to capital lockup and fiat/crypto friction between
  platforms — a structural, not purely informational, barrier.
- The most careful microstructure study,
  [Cheng, Yang & Zou (UCLA)](https://arxiv.org/abs/2605.00864), rebuilt
  order books from 75M snapshots across 173 NBA games and 3,042 markets:
  **single-market arbitrage was exceedingly rare** — 7 executable episodes
  total, median duration 3.6 seconds (at the edge of their own polling
  resolution, so likely an overestimate of persistence), total capped
  profit across all of them: $210. Cross-market (combinatorial) arbitrage
  was more frequent (290 episodes) but **76.9% were capped by order-book
  depth to ~14.8 shares** — "structurally bounded by liquidity, confining
  risk-free extraction strictly to the retail scale."
- One estimate ([via Finance Magnates](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/),
  citing an academic working paper) put total arbitrage extraction from
  Polymarket at ~$40M over a year, with 14 of the top 20 most profitable
  wallets reportedly being bots (anecdotal sourcing on that specific claim).

**Read together**: real, but the rigorous order-book-level study paints a
much smaller, much faster, much more liquidity-capped picture than the
"1-5% edges, 15-30 second windows" framing pushed by bot-selling sites —
treat that framing as marketing.

### Favorite-longshot bias / calibration — real, but is compensation for risk, not free money

- **[Bürgi, Deng & Whelan (GWU), "Makers and Takers"](https://www2.gwu.edu/~forcpgm/2026-001.pdf)**,
  300K+ Kalshi contracts: average pre-fee contract return is -20% even
  though the market is zero-sum before fees, driven by longshot
  overpricing. Critically, **takers lose ~32% on longshot-side contracts on
  average, makers only ~10%** — evidence this is compensation for
  liquidity provision / adverse selection, not exploitable alpha for a
  retail taker fading longshots directly.
- **[Le, "Decomposing Crowd Wisdom"](https://arxiv.org/abs/2602.19520)**
  (353M trades, both platforms): **Politics is persistently
  underconfident** (a 70¢ political contract a week out implies a true
  probability closer to 83%); **Sports is well-calibrated short-term but
  underconfident past ~1 month**; **Weather/Entertainment show the opposite
  pattern** (overpriced at short horizons); **Crypto is close to neutral**
  on both platforms.
- **[Moshrefi (Princeton)](https://arxiv.org/abs/2607.14430)**, 23M Kalshi
  sports trades: calibration is near-perfect mid-game, becomes
  step-function-like in the final 10 minutes (traders pay an "insurance
  premium" hedging late). Separately, **Kalshi parlays are systematically
  overpriced** relative to their well-calibrated individual legs, with the
  markup growing geometrically in leg count — a cleaner, more mechanical
  inefficiency than trying to out-forecast the crowd on single legs.

### Market-making — closer to underwriting insurance than passive income

- Polymarket runs a Liquidity Rewards program (resting-order-based, paid
  regardless of fill, $1 daily minimum, no rollover) plus Maker Rebates on
  fills ([docs](https://docs.polymarket.com/programs/liquidity-rewards)).
  Kalshi runs a designated Market Maker Program with quote-uptime
  obligations ([help.kalshi.com](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi)).
- **[Dubach's tick-level Polymarket study](https://arxiv.org/abs/2604.24366)**
  (30B order-book events, 52 days, 600 markets): median half-spread ~200bps
  at 40-60¢, ballooning to 1,300-1,800bps below 10¢ — an order of magnitude
  wider than liquid equities, attributed to genuine inventory-risk limits
  (bounded upside on cheap binary contracts), not just behavioral bias.
- Given makers still average losses on longshot-side contracts (per Whelan
  et al. above), and informal reports (a Reddit post referencing a
  self-built Polymarket MM bot going from +$5K to giving it back to stale
  quotes / adverse selection) — market-making here reads as **compensated
  risk-bearing on binary outcomes, not free liquidity-mining income**.

### Speed edges — real, but not accessible to a retail API trader

The same UCLA NBA study found genuine mispricings correct with a median
3.6-second lifespan — below what a typical retail/API bot can act on.
Polymarket itself added fees on 15-minute crypto contracts in Jan 2026
specifically because "sophisticated firms exploited millisecond gaps"
between its prices and Binance's (per Finance Magnates) — the exchange
acknowledging and taxing away exactly this edge.

## Costs that erode all of the above

| Cost | Kalshi | Polymarket |
|---|---|---|
| Trading fee | `ceil(0.07 × C × P × (1−P))` — peaks ~1.75% of notional at 50¢, ~0 at extremes ([source](https://marketmath.io/platforms/kalshi), cross-checked against the [CFTC-filed schedule](https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf)) | Dynamic taker fee, $1.00-$1.75/100 shares, $0 on many geopolitical/world markets; $0 maker fees |
| Settlement cost | Regulated bank rails, no gas | Near-zero Polygon gas; bridging can run $1-$20+ in congestion |
| Spread, liquid market | — | ~200bps half-spread at mid-price (top-100 panel) |
| Spread, illiquid/extreme | Median spread ballooned to 7,532bps post-game (stale quotes) in the NBA study | 1,300-1,800bps below the 10¢ decile |

The dominant real-world cost for most strategies is spread + limited depth,
not the posted fee schedule — a strategy needs to clear spreads 5-10x wider
than liquid equities on anything but the most active markets, and edges
larger than a few percent tend to be capped by the size actually
executable before quotes move.

## What a disciplined, evidence-based strategy would actually look like

(Not implemented by default in this project — a direction, not a
recommendation to pursue any of these without your own further diligence.)

1. Target the maker/taker asymmetry directly (careful, hedged liquidity
   provision) rather than fighting it with directional longshot bets.
2. Condition explicitly on category and time-to-expiry, since calibration
   direction reverses between domains and even within one domain across a
   contract's life.
3. Treat parlay/combinatorial mispricing as a distinct, more mechanical
   opportunity from single-leg forecasting.
4. Size for the liquidity that actually exists, not the theoretical edge —
   the UCLA paper's uncapped-vs-capped profit gap ($2,032 theoretical vs.
   $559 executable) is the clearest illustration of how much published
   "inefficiency" overstates what's extractable at retail scale.
5. Treat any LLM confidence score as needing calibration correction before
   it feeds a Kelly-style sizer — an overconfident model feeding a Kelly
   sizer over-bets systematically, which is exactly what this project's
   `risk/manager.py` guards against by never trusting the model's own
   suggested size and applying a fractional-Kelly multiplier on top.

## Honest base-rate expectation

Given (a) spreads 5-10x wider than equities on anything but the most liquid
markets, (b) genuine arbitrage measured in seconds and single/double-digit
dollars once liquidity-capped, (c) the one rigorous live-money LLM trial
losing 16-31% on Kalshi over 57 days, and (d) even the academically
documented biases being partly compensation for risk rather than free
alpha — a retail algorithmic trader without a genuine informational or
infrastructure edge should expect to **roughly break even to modestly lose
money after costs**, with exceptions concentrated in narrow,
labor-intensive niches (careful hedged market-making, parlay-mispricing
arbitrage, disciplined cross-platform basis trades sized to real depth)
rather than a general "trade what an LLM says looks mispriced" strategy.

## Full source list

- [Krause: Evidence of Persistent Arbitrage in Prediction Markets](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6905683)
- [Krause: Basis Risk Arbitrage — Clarity Act case study](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6984101)
- [Ng, Peng, Tao, Zhou: Price Discovery and Trading in Modern Prediction Markets](https://papers.ssrn.com/sol3/Delivery.cfm/5331995.pdf?abstractid=5331995&mirid=1)
- [Cheng, Yang, Zou: Arbitrage Analysis in Polymarket NBA Markets](https://arxiv.org/abs/2605.00864)
- [Bürgi, Deng, Whelan: Makers and Takers — Kalshi economics](https://www2.gwu.edu/~forcpgm/2026-001.pdf) ([VoxEU summary](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market))
- [Le: Decomposing Crowd Wisdom — Domain-Specific Calibration Dynamics](https://arxiv.org/abs/2602.19520)
- [Moshrefi: Prices, Probabilities, and Parlays](https://arxiv.org/abs/2607.14430)
- [Dubach: The Anatomy of a Decentralized Prediction Market](https://arxiv.org/abs/2604.24366)
- [Qin & Yang: Polymarket-v1 Database](https://arxiv.org/abs/2606.04217)
- [Nel: KalshiBench](https://arxiv.org/abs/2512.16030)
- [Karger et al.: ForecastBench](https://arxiv.org/abs/2409.19839)
- [Zhang et al.: Prediction Arena](https://arxiv.org/abs/2604.07355)
- [Nechepurenko & Shuvalov: Foresight Arena](https://arxiv.org/abs/2605.00420)
- [Barot & Borkhatariya: PolySwarm](https://arxiv.org/abs/2604.03888) — a system-design paper with no disclosed live-capital results; treat any marketing built on it skeptically until real numbers are published.
- [Finance Magnates: Prediction Markets Are Turning Into a Bot Playground](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [Yahoo Finance: trader lost $2M on Polymarket](https://finance.yahoo.com/news/trader-lost-2-million-polymarket-133015895.html) — not a bot, but instructive: >50% win rate isn't sufficient without disciplined sizing.
- [Polymarket Liquidity Rewards docs](https://docs.polymarket.com/programs/liquidity-rewards)
- [Kalshi Market Maker Program](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi)
