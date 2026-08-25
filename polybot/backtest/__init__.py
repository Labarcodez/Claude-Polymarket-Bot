"""Backtesting: replay historical market data through the *real*
scanner-filter (passes_filters) and risk-management (RiskManager) code --
only the analysis step (what would have decided BUY_YES/BUY_NO/HOLD/
NO_TRADE) is swapped for a pluggable strategy. This is deliberate: reusing
the production risk/sizing/exit code means a backtest result reflects the
same rules that would actually govern a live run, instead of a parallel
reimplementation that could quietly drift from what's really deployed.

See docs/BACKTESTING.md for the historical data format, how to fetch real
data, and -- importantly -- the look-ahead-bias trap specific to
backtesting an LLM strategy against markets whose outcome the model may
already "know" from training data.
"""
