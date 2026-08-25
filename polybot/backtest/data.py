"""Historical data loading for the backtest engine, plus a synthetic
demo-data generator for exercising the engine without any real market data.

Two CSV files, kept separate because that's how you'd realistically collect
them from a real venue too: a time series of market snapshots (collected
periodically while markets are open) and a one-time resolutions table
(collected once each market settles). See docs/BACKTESTING.md for the exact
column schemas and how to populate `snapshots` from Polymarket's/Kalshi's
own historical-price endpoints.
"""
from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..engine.models import MarketSnapshot


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_snapshots(path: str) -> List[Tuple[datetime, List[MarketSnapshot]]]:
    """Load a snapshots CSV into a time-ordered list of (timestamp, markets
    active at that timestamp) -- the unit of replay the engine advances by
    one at a time, exactly mirroring one `TradingEngine.run_cycle()`.

    Required columns: condition_id, ts, yes_price.
    Optional: venue, question, slug, yes_token_id, no_token_id, no_price,
    best_bid_yes, best_ask_yes, spread, volume_24hr, liquidity, end_date,
    tags (semicolon-separated).
    """
    grouped: Dict[datetime, List[MarketSnapshot]] = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = _parse_dt(row["ts"])
            end_date = _parse_dt(row["end_date"]) if row.get("end_date") else None
            yes_price = float(row["yes_price"])
            tags = [t for t in (row.get("tags") or "").split(";") if t]

            def _opt_float(key: str) -> Optional[float]:
                v = row.get(key)
                return float(v) if v not in (None, "") else None

            snap = MarketSnapshot(
                venue=row.get("venue") or "polymarket",
                condition_id=row["condition_id"],
                question=row.get("question", ""),
                slug=row.get("slug") or row["condition_id"],
                yes_token_id=row.get("yes_token_id") or row["condition_id"],
                no_token_id=row.get("no_token_id") or row["condition_id"],
                yes_price=yes_price,
                no_price=_opt_float("no_price") if _opt_float("no_price") is not None else round(1 - yes_price, 6),
                best_bid_yes=_opt_float("best_bid_yes"),
                best_ask_yes=_opt_float("best_ask_yes"),
                spread=_opt_float("spread"),
                volume_24hr=_opt_float("volume_24hr") or 0.0,
                liquidity=_opt_float("liquidity") or 0.0,
                end_date=end_date,
                tags=tags,
            )
            grouped[ts].append(snap)
    return sorted(grouped.items(), key=lambda kv: kv[0])


def load_resolutions(path: str) -> Dict[str, Tuple[str, datetime]]:
    """Load a resolutions CSV: condition_id, outcome (YES/NO), resolved_ts."""
    out: Dict[str, Tuple[str, datetime]] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[row["condition_id"]] = (row["outcome"].strip().upper(), _parse_dt(row["resolved_ts"]))
    return out


def generate_synthetic_dataset(
    snapshots_path: str,
    resolutions_path: str,
    num_markets: int = 25,
    days: int = 30,
    points_per_day: int = 4,
    seed: int = 42,
) -> Tuple[str, str]:
    """Generate a SYNTHETIC, calibrated-by-construction demo dataset --
    NOT real market data, and not a source of evidence about real
    trading edge. Each market's price follows a driftless, bounded random
    walk (no momentum, no exploitable autocorrelation by construction),
    and its final resolution is sampled with probability equal to its own
    last quoted price -- i.e. the synthetic market is, by construction,
    perfectly calibrated and informationally efficient.

    This makes it a legitimate *engine sanity check*, not a demo of
    profitability: no strategy should show a consistent positive edge
    against data built this way. If one does in a large enough sample,
    that's a red flag pointing at a bug in the backtest engine (e.g. a
    look-ahead leak) -- not real alpha. See docs/BACKTESTING.md.
    """
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    Path(snapshots_path).parent.mkdir(parents=True, exist_ok=True)
    Path(resolutions_path).parent.mkdir(parents=True, exist_ok=True)

    snap_fields = [
        "condition_id", "venue", "question", "slug", "ts", "yes_price", "no_price",
        "best_bid_yes", "best_ask_yes", "spread", "volume_24hr", "liquidity", "end_date", "tags",
    ]
    res_fields = ["condition_id", "outcome", "resolved_ts"]

    with open(snapshots_path, "w", newline="") as sf, open(resolutions_path, "w", newline="") as rf:
        snap_writer = csv.DictWriter(sf, fieldnames=snap_fields)
        snap_writer.writeheader()
        res_writer = csv.DictWriter(rf, fieldnames=res_fields)
        res_writer.writeheader()

        for i in range(num_markets):
            condition_id = f"SYN-{i:03d}"
            price = rng.uniform(0.15, 0.85)
            end_offset_days = rng.randint(5, days)
            end_date = start + timedelta(days=end_offset_days)
            volume = rng.uniform(5_000, 100_000)
            liquidity = rng.uniform(2_000, 50_000)
            n_points = max(1, end_offset_days * points_per_day)

            for p in range(n_points):
                ts = start + timedelta(hours=p * (24 / points_per_day))
                price = min(max(price + rng.gauss(0, 0.01), 0.02), 0.98)  # driftless random walk
                spread = rng.uniform(0.01, 0.04)
                bid = max(price - spread / 2, 0.01)
                ask = min(price + spread / 2, 0.99)
                snap_writer.writerow({
                    "condition_id": condition_id, "venue": "polymarket",
                    "question": f"[SYNTHETIC DEMO DATA] Market #{i}", "slug": condition_id,
                    "ts": ts.isoformat(), "yes_price": round(price, 4), "no_price": round(1 - price, 4),
                    "best_bid_yes": round(bid, 4), "best_ask_yes": round(ask, 4),
                    "spread": round(ask - bid, 4), "volume_24hr": round(volume, 2),
                    "liquidity": round(liquidity, 2), "end_date": end_date.isoformat(), "tags": "Synthetic",
                })

            # Calibrated by construction: P(resolves YES) == the market's
            # own final price. This is what makes it a null-hypothesis
            # dataset rather than a profitability demo.
            outcome = "YES" if rng.random() < price else "NO"
            res_writer.writerow({
                "condition_id": condition_id, "outcome": outcome, "resolved_ts": end_date.isoformat(),
            })

    return snapshots_path, resolutions_path
