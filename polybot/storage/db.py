"""Local SQLite ledger: every decision, order, position, and daily stat the
bot produces, whether paper (dry_run) or real (live). This is the bot's
memory across restarts and the source of truth for `polybot status`.
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
    condition_id TEXT NOT NULL,
    slug TEXT,
    question TEXT,
    action TEXT,
    fair_value_probability REAL,
    confidence REAL,
    market_price REAL,
    reasoning TEXT,
    risk_flags TEXT,
    executed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
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
    date TEXT PRIMARY KEY,
    trades_count INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    def __init__(self, db_path: str = "data/polybot.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

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

    def record_decision(self, condition_id: str, slug: str, decision: TradeDecision, market_price: float, executed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO decisions
                   (ts, condition_id, slug, question, action, fair_value_probability,
                    confidence, market_price, reasoning, risk_flags, executed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(), condition_id, slug, None, decision.action.value,
                    decision.fair_value_probability, decision.confidence, market_price,
                    decision.reasoning, json.dumps(decision.risk_flags), int(executed),
                ),
            )

    # ---- orders & positions -------------------------------------------------

    def record_order(self, plan: OrderPlan, status: str, order_id: Optional[str], dry_run: bool, raw_response: Any = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders
                   (ts, condition_id, token_id, side, outcome, size_usd, limit_price,
                    order_type, status, order_id, dry_run, raw_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(), plan.condition_id, plan.token_id, plan.side, plan.outcome,
                    plan.size_usd, plan.limit_price, plan.order_type, status, order_id,
                    int(dry_run), json.dumps(raw_response, default=str) if raw_response else None,
                ),
            )

    def open_position(self, plan: OrderPlan, fill_price: float, shares: float, end_date: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO positions
                   (condition_id, token_id, outcome, question, shares, avg_cost, opened_ts, end_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                   ON CONFLICT(condition_id) DO UPDATE SET
                     shares = shares + excluded.shares,
                     avg_cost = ((shares * avg_cost) + (excluded.shares * excluded.avg_cost)) / (shares + excluded.shares)
                   """,
                (
                    plan.condition_id, plan.token_id, plan.outcome, plan.question,
                    shares, fill_price, _now_iso(), end_date,
                ),
            )
        self.increment_today_trade()

    def close_position(self, condition_id: str, fill_price: float, reason: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT shares, avg_cost FROM positions WHERE condition_id = ? AND status = 'open'",
                (condition_id,),
            ).fetchone()
            if row is None:
                return
            shares, avg_cost = row["shares"], row["avg_cost"]
            realized = (fill_price - avg_cost) * shares
            conn.execute(
                """UPDATE positions
                   SET status='closed', closed_ts=?, close_reason=?, realized_pnl=?
                   WHERE condition_id=?""",
                (_now_iso(), reason, realized, condition_id),
            )
        self.add_realized_pnl(realized)
        self.increment_today_trade()

    def get_open_positions(self) -> List[OpenPosition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
        positions = []
        for r in rows:
            positions.append(
                OpenPosition(
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

    # ---- daily stats ---------------------------------------------------------

    def _ensure_today_row(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO daily_stats (date, trades_count, realized_pnl) VALUES (?, 0, 0)",
            (_today(),),
        )

    def increment_today_trade(self) -> None:
        with self._connect() as conn:
            self._ensure_today_row(conn)
            conn.execute(
                "UPDATE daily_stats SET trades_count = trades_count + 1 WHERE date = ?", (_today(),)
            )

    def add_realized_pnl(self, amount: float) -> None:
        with self._connect() as conn:
            self._ensure_today_row(conn)
            conn.execute(
                "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
                (amount, _today()),
            )

    def get_today_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            self._ensure_today_row(conn)
            row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (_today(),)).fetchone()
        return {"trades_count": row["trades_count"], "realized_pnl": row["realized_pnl"]}

    def get_all_time_realized_pnl(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM daily_stats").fetchone()
        return float(row["total"])
