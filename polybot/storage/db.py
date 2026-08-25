"""Local SQLite ledger: every decision, order, position, and daily stat the
bot produces, whether paper (dry_run) or real (live), tagged by which venue
(Polymarket or Kalshi) produced it. This is the bot's memory across restarts
and the source of truth for `polybot status`.

Multi-venue note: `condition_id` collisions across venues are not realistic
(Polymarket's are 66-char hex conditionIds, Kalshi's are short tickers like
"KXPRES-24-DJT") so decisions/orders/positions just carry a `venue` column
without needing a composite key. `daily_stats` is keyed by date alone in
older databases, which *would* silently merge two venues' stats on the same
calendar day -- `_migrate` rebuilds that one table onto a (date, venue)
primary key on first run against an old database.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..engine.models import OpenPosition, OrderPlan, TradeDecision

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'polymarket',
    condition_id TEXT NOT NULL,
    slug TEXT,
    question TEXT,
    action TEXT,
    fair_value_probability REAL,
    confidence REAL,
    market_price REAL,
    reasoning TEXT,
    risk_flags TEXT,
    executed INTEGER DEFAULT 0,
    edge_at_execution REAL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'polymarket',
    condition_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    outcome TEXT,
    size_usd REAL,
    limit_price REAL,
    order_type TEXT,
    status TEXT,
    order_id TEXT,
    dry_run INTEGER,
    raw_response TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    condition_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL DEFAULT 'polymarket',
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    question TEXT,
    shares REAL NOT NULL,
    avg_cost REAL NOT NULL,
    opened_ts TEXT NOT NULL,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    closed_ts TEXT,
    close_reason TEXT,
    realized_pnl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'polymarket',
    trades_count INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    PRIMARY KEY (date, venue)
);
"""


def _now_iso(as_of: Optional[datetime] = None) -> str:
    return (as_of or datetime.now(timezone.utc)).isoformat()


def _today(as_of: Optional[datetime] = None) -> str:
    """The calendar date daily_stats buckets against. `as_of` lets the
    backtest engine (polybot/backtest/) replay historical dates through
    these same daily-limit-tracking methods instead of always bucketing
    into the real wall-clock date -- without it, a multi-day backtest would
    silently collapse every simulated day's trades into a single "today"
    row, making max_daily_trades/max_daily_loss meaningless in replay."""
    return (as_of or datetime.now(timezone.utc)).date().isoformat()


class Database:
    def __init__(self, db_path: str = "data/polybot.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Idempotent, additive migrations for databases created by earlier
        (single-venue) versions of this project."""
        with self._connect() as conn:
            for table in ("decisions", "orders", "positions"):
                cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "venue" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket'")

            decisions_cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
            if "edge_at_execution" not in decisions_cols:
                conn.execute("ALTER TABLE decisions ADD COLUMN edge_at_execution REAL")

            stats_cols = {row["name"] for row in conn.execute("PRAGMA table_info(daily_stats)").fetchall()}
            if "venue" not in stats_cols:
                # daily_stats was keyed by `date` alone -- rebuild onto a
                # (date, venue) primary key so two venues on the same
                # calendar day don't silently share one counter.
                conn.executescript(
                    """
                    ALTER TABLE daily_stats RENAME TO daily_stats_old;
                    CREATE TABLE daily_stats (
                        date TEXT NOT NULL,
                        venue TEXT NOT NULL DEFAULT 'polymarket',
                        trades_count INTEGER DEFAULT 0,
                        realized_pnl REAL DEFAULT 0,
                        PRIMARY KEY (date, venue)
                    );
                    INSERT INTO daily_stats (date, venue, trades_count, realized_pnl)
                        SELECT date, 'polymarket', trades_count, realized_pnl FROM daily_stats_old;
                    DROP TABLE daily_stats_old;
                    """
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- decisions ---------------------------------------------------------

    def record_decision(
        self, condition_id: str, slug: str, decision: TradeDecision, market_price: float,
        executed: bool, venue: str = "polymarket", question: str = "", as_of: Optional[datetime] = None,
        edge_at_execution: Optional[float] = None,
    ) -> None:
        """`edge_at_execution` is the signed (true_prob - price) edge the risk
        manager actually computed for the traded side (see
        RiskManager._resolve_side) -- distinct from `market_price`, which is
        always a YES-ask-anchored implied-P(YES) figure used as the Brier
        baseline regardless of trade side. Callers that don't know/compute
        this (e.g. live TradingEngine, which doesn't currently expose it)
        can omit it; it's only populated by the backtest engine today."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO decisions
                   (ts, venue, condition_id, slug, question, action, fair_value_probability,
                    confidence, market_price, reasoning, risk_flags, executed, edge_at_execution)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(as_of), venue, condition_id, slug, question, decision.action.value,
                    decision.fair_value_probability, decision.confidence, market_price,
                    decision.reasoning, json.dumps(decision.risk_flags), int(executed),
                    edge_at_execution,
                ),
            )

    def get_recent_decisions(self, venue: Optional[str] = None, limit: Optional[int] = 20) -> List[Dict[str, Any]]:
        """Most recent decisions first, for `polybot decisions` -- the
        auditable record of what Claude actually said about each market,
        whether or not the risk manager acted on it. `limit=None` returns
        every decision (used by polybot/backtest/metrics.py)."""
        query = "SELECT * FROM decisions"
        params: tuple = ()
        if venue:
            query += " WHERE venue = ?"
            params = (venue,)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params = params + (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ---- orders & positions -------------------------------------------------

    def record_order(
        self, plan: OrderPlan, status: str, order_id: Optional[str], dry_run: bool,
        raw_response: Any = None, as_of: Optional[datetime] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders
                   (ts, venue, condition_id, token_id, side, outcome, size_usd, limit_price,
                    order_type, status, order_id, dry_run, raw_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(as_of), plan.venue, plan.condition_id, plan.token_id, plan.side, plan.outcome,
                    plan.size_usd, plan.limit_price, plan.order_type, status, order_id,
                    int(dry_run), json.dumps(raw_response, default=str) if raw_response else None,
                ),
            )

    def open_position(
        self, plan: OrderPlan, fill_price: float, shares: float, end_date: Optional[str],
        as_of: Optional[datetime] = None,
    ) -> None:
        """Insert a new position, or add to an existing *open* one at that
        condition_id. Deliberately does NOT merge into a previously *closed*
        row at the same condition_id (SQLite's ON CONFLICT can't easily
        branch on the existing row's status) -- re-entering a market the
        bot held and already exited is a brand-new position, not a
        continuation of the old one, and treating it as a continuation used
        to silently leave status='closed' on a row that actually held live
        shares again, hiding it from get_open_positions() (and therefore
        from ExitManager and RiskManager) entirely."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT status, outcome, shares, avg_cost FROM positions WHERE condition_id = ?",
                (plan.condition_id,),
            ).fetchone()

            if existing is not None and existing["status"] == "open":
                if existing["outcome"] != plan.outcome:
                    # Defense in depth: RiskManager.plan_entry_order already
                    # refuses a second entry into a market it holds an open
                    # position in (regardless of side), so this should be
                    # unreachable in normal operation -- but this table's
                    # PK is condition_id alone (one row per market, not per
                    # outcome), so if that upstream guard were ever loosened
                    # or bypassed, blending a YES fill's shares/avg_cost
                    # into an open NO row (or vice versa) would silently
                    # corrupt the position's cost basis with no error. Fail
                    # loudly instead.
                    raise ValueError(
                        f"open_position: condition_id={plan.condition_id} already has an open "
                        f"{existing['outcome']} position; refusing to blend in a {plan.outcome} fill "
                        "under the same row (this table holds one outcome per market)."
                    )
                new_shares = existing["shares"] + shares
                new_avg_cost = (existing["shares"] * existing["avg_cost"] + shares * fill_price) / new_shares
                conn.execute(
                    "UPDATE positions SET shares = ?, avg_cost = ? WHERE condition_id = ?",
                    (new_shares, new_avg_cost, plan.condition_id),
                )
            else:
                conn.execute(
                    """INSERT INTO positions
                       (condition_id, venue, token_id, outcome, question, shares, avg_cost,
                        opened_ts, end_date, status, closed_ts, close_reason, realized_pnl)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, 0)
                       ON CONFLICT(condition_id) DO UPDATE SET
                         venue = excluded.venue, token_id = excluded.token_id, outcome = excluded.outcome,
                         question = excluded.question, shares = excluded.shares, avg_cost = excluded.avg_cost,
                         opened_ts = excluded.opened_ts, end_date = excluded.end_date,
                         status = 'open', closed_ts = NULL, close_reason = NULL, realized_pnl = 0
                       """,
                    (
                        plan.condition_id, plan.venue, plan.token_id, plan.outcome, plan.question,
                        shares, fill_price, _now_iso(as_of), end_date,
                    ),
                )
        self.increment_today_trade(plan.venue, as_of=as_of)

    def close_position(
        self, condition_id: str, fill_price: float, reason: str, as_of: Optional[datetime] = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT venue, shares, avg_cost FROM positions WHERE condition_id = ? AND status = 'open'",
                (condition_id,),
            ).fetchone()
            if row is None:
                return
            venue, shares, avg_cost = row["venue"], row["shares"], row["avg_cost"]
            realized = (fill_price - avg_cost) * shares
            conn.execute(
                """UPDATE positions
                   SET status='closed', closed_ts=?, close_reason=?, realized_pnl=?
                   WHERE condition_id=?""",
                (_now_iso(as_of), reason, realized, condition_id),
            )
        self.add_realized_pnl(realized, venue, as_of=as_of)
        # Deliberately does NOT call increment_today_trade() here: trades_count
        # feeds RiskManager.max_daily_trades, which -- per its own docstring --
        # gates new *entries* only; exits must stay unmetered by it, or a busy
        # day of legitimate stop-loss/take-profit exits would burn through the
        # entry budget and block new entries for the rest of the day for a
        # reason that has nothing to do with entries at all.

    def get_open_positions(self, venue: Optional[str] = None) -> List[OpenPosition]:
        with self._connect() as conn:
            if venue:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE status = 'open' AND venue = ?", (venue,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
        positions = []
        for r in rows:
            positions.append(
                OpenPosition(
                    venue=r["venue"],
                    condition_id=r["condition_id"],
                    token_id=r["token_id"],
                    outcome=r["outcome"],
                    question=r["question"] or "",
                    shares=r["shares"],
                    avg_cost=r["avg_cost"],
                    opened_ts=datetime.fromisoformat(r["opened_ts"]),
                    end_date=datetime.fromisoformat(r["end_date"]) if r["end_date"] else None,
                )
            )
        return positions

    def get_closed_positions(self, venue: Optional[str] = None) -> List[Dict[str, Any]]:
        """Raw closed-position rows, oldest-closed-first. Unlike
        get_open_positions this returns dicts, not OpenPosition, since a
        closed row carries fields (realized_pnl, closed_ts, close_reason)
        that model doesn't have. Used by polybot/backtest/metrics.py to
        build an equity curve and win-rate from a backtest run."""
        query = "SELECT * FROM positions WHERE status = 'closed'"
        params: tuple = ()
        if venue:
            query += " AND venue = ?"
            params = (venue,)
        query += " ORDER BY closed_ts"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ---- daily stats ---------------------------------------------------------

    def _ensure_today_row(self, conn: sqlite3.Connection, venue: str, as_of: Optional[datetime] = None) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO daily_stats (date, venue, trades_count, realized_pnl) VALUES (?, ?, 0, 0)",
            (_today(as_of), venue),
        )

    def increment_today_trade(self, venue: str = "polymarket", as_of: Optional[datetime] = None) -> None:
        with self._connect() as conn:
            self._ensure_today_row(conn, venue, as_of)
            conn.execute(
                "UPDATE daily_stats SET trades_count = trades_count + 1 WHERE date = ? AND venue = ?",
                (_today(as_of), venue),
            )

    def add_realized_pnl(self, amount: float, venue: str = "polymarket", as_of: Optional[datetime] = None) -> None:
        with self._connect() as conn:
            self._ensure_today_row(conn, venue, as_of)
            conn.execute(
                "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ? AND venue = ?",
                (amount, _today(as_of), venue),
            )

    def get_today_stats(self, venue: str = "polymarket", as_of: Optional[datetime] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            self._ensure_today_row(conn, venue, as_of)
            row = conn.execute(
                "SELECT * FROM daily_stats WHERE date = ? AND venue = ?", (_today(as_of), venue)
            ).fetchone()
        return {"trades_count": row["trades_count"], "realized_pnl": row["realized_pnl"]}

    def get_all_time_realized_pnl(self, venue: Optional[str] = None) -> float:
        with self._connect() as conn:
            if venue:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM daily_stats WHERE venue = ?", (venue,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM daily_stats").fetchone()
        return float(row["total"])
