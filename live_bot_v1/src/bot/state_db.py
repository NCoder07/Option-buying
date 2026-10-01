"""
state_db.py — SQLite-backed persistent state for the live bot.

Tables
------
  positions   — one row per open option position (persists overnight)
  orders      — every order sent (idempotency + audit)
  daily_state — per-side (CE/PE) daily status flags

All datetimes stored as ISO-8601 strings in IST.
The DB file path is provided at construction time; the bot never hard-codes it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,          -- YYYY-MM-DD
    side          TEXT NOT NULL,          -- CE | PE
    symbol        TEXT NOT NULL,
    expiry        TEXT NOT NULL,          -- YYYY-MM-DD
    strike        INTEGER NOT NULL,
    qty           INTEGER NOT NULL,
    avg_fill_price REAL NOT NULL,
    sl_price      REAL NOT NULL,
    entry_order_id TEXT NOT NULL,
    entry_time    TEXT,
    status        TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    closed_at     TEXT,
    exit_reason   TEXT,
    exit_order_id TEXT,
    exit_fill_price REAL,
    extra         TEXT DEFAULT '{}'      -- JSON extra fields
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT UNIQUE NOT NULL,
    date          TEXT NOT NULL,
    side          TEXT,
    symbol        TEXT,
    direction     TEXT,                   -- BUY | SELL
    qty           INTEGER,
    submitted_price REAL,
    fill_price    REAL,
    status        TEXT,
    tag           TEXT,
    sent_at       TEXT,
    filled_at     TEXT,
    latency_ms    REAL,
    raw_response  TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS daily_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    side          TEXT NOT NULL,          -- CE | PE
    expiry        TEXT,
    strike        INTEGER,
    symbol        TEXT,
    p0            REAL,
    trigger       REAL,
    reference_ts  TEXT,
    reference_latency_ms REAL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING | SELECTED | TRIGGERED | ENTERED | CLOSED | NO_TRADE | MISSED_REFERENCE
    no_trade_reason TEXT,
    extra         TEXT DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_state ON daily_state(date, side);
"""


@dataclass
class PositionRecord:
    date: str
    side: str
    symbol: str
    expiry: str
    strike: int
    qty: int
    avg_fill_price: float
    sl_price: float
    entry_order_id: str
    entry_time: Optional[str] = None
    status: str = "OPEN"
    closed_at: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_fill_price: Optional[float] = None
    extra: dict = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class OrderRecord:
    order_id: str
    date: str
    side: Optional[str]
    symbol: Optional[str]
    direction: str                # BUY | SELL
    qty: int
    submitted_price: float
    fill_price: float = 0.0
    status: str = "PENDING"
    tag: Optional[str] = None
    sent_at: Optional[str] = None
    filled_at: Optional[str] = None
    latency_ms: float = 0.0
    raw_response: dict = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class DailyStateRecord:
    date: str
    side: str                     # CE | PE
    expiry: Optional[str] = None
    strike: Optional[int] = None
    symbol: Optional[str] = None
    p0: Optional[float] = None
    trigger: Optional[float] = None
    reference_ts: Optional[str] = None
    reference_latency_ms: Optional[float] = None
    status: str = "PENDING"
    no_trade_reason: Optional[str] = None
    extra: dict = field(default_factory=dict)
    id: Optional[int] = None


class StateDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        logger.info("StateDB initialised at %s", self._db_path)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def upsert_position(self, pos: PositionRecord) -> int:
        extra_json = json.dumps(pos.extra)
        with self._conn() as conn:
            if pos.id is not None:
                conn.execute(
                    """UPDATE positions SET status=?, closed_at=?, exit_reason=?,
                       exit_order_id=?, exit_fill_price=?, extra=?, sl_price=?
                       WHERE id=?""",
                    (pos.status, pos.closed_at, pos.exit_reason,
                     pos.exit_order_id, pos.exit_fill_price, extra_json,
                     pos.sl_price, pos.id),
                )
                return pos.id
            else:
                cur = conn.execute(
                    """INSERT INTO positions
                       (date, side, symbol, expiry, strike, qty, avg_fill_price,
                        sl_price, entry_order_id, entry_time, status, extra)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pos.date, pos.side, pos.symbol, pos.expiry, pos.strike,
                     pos.qty, pos.avg_fill_price, pos.sl_price,
                     pos.entry_order_id, pos.entry_time, pos.status, extra_json),
                )
                return cur.lastrowid

    def get_open_positions(self) -> list[PositionRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='OPEN'"
            ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_positions_for_date(self, date: str) -> list[PositionRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE date=?", (date,)
            ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def _row_to_position(self, r: sqlite3.Row) -> PositionRecord:
        return PositionRecord(
            id=r["id"], date=r["date"], side=r["side"], symbol=r["symbol"],
            expiry=r["expiry"], strike=r["strike"], qty=r["qty"],
            avg_fill_price=r["avg_fill_price"], sl_price=r["sl_price"],
            entry_order_id=r["entry_order_id"], entry_time=r["entry_time"],
            status=r["status"], closed_at=r["closed_at"],
            exit_reason=r["exit_reason"], exit_order_id=r["exit_order_id"],
            exit_fill_price=r["exit_fill_price"],
            extra=json.loads(r["extra"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def insert_order(self, rec: OrderRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO orders
                   (order_id, date, side, symbol, direction, qty,
                    submitted_price, fill_price, status, tag,
                    sent_at, filled_at, latency_ms, raw_response)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.order_id, rec.date, rec.side, rec.symbol, rec.direction,
                 rec.qty, rec.submitted_price, rec.fill_price, rec.status,
                 rec.tag, rec.sent_at, rec.filled_at, rec.latency_ms,
                 json.dumps(rec.raw_response)),
            )

    def update_order(self, rec: OrderRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE orders SET fill_price=?, status=?, filled_at=?,
                   latency_ms=?, raw_response=? WHERE order_id=?""",
                (rec.fill_price, rec.status, rec.filled_at,
                 rec.latency_ms, json.dumps(rec.raw_response), rec.order_id),
            )

    def get_orders_for_date(self, date: str) -> list[OrderRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE date=?", (date,)
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def order_exists(self, order_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
        return row is not None

    def _row_to_order(self, r: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=r["id"], order_id=r["order_id"], date=r["date"],
            side=r["side"], symbol=r["symbol"], direction=r["direction"],
            qty=r["qty"], submitted_price=r["submitted_price"],
            fill_price=r["fill_price"] or 0.0, status=r["status"],
            tag=r["tag"], sent_at=r["sent_at"], filled_at=r["filled_at"],
            latency_ms=r["latency_ms"] or 0.0,
            raw_response=json.loads(r["raw_response"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Daily state
    # ------------------------------------------------------------------

    def get_or_create_daily_state(self, date: str, side: str) -> DailyStateRecord:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_state WHERE date=? AND side=?",
                (date, side)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO daily_state (date, side) VALUES (?,?)",
                    (date, side)
                )
                return DailyStateRecord(date=date, side=side)
            return self._row_to_daily(row)

    def update_daily_state(self, rec: DailyStateRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO daily_state
                   (id, date, side, expiry, strike, symbol, p0, trigger,
                    reference_ts, reference_latency_ms, status,
                    no_trade_reason, extra)
                   VALUES (
                     (SELECT id FROM daily_state WHERE date=? AND side=?),
                     ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.date, rec.side,
                 rec.date, rec.side, rec.expiry, rec.strike, rec.symbol,
                 rec.p0, rec.trigger, rec.reference_ts, rec.reference_latency_ms,
                 rec.status, rec.no_trade_reason, json.dumps(rec.extra)),
            )

    def _row_to_daily(self, r: sqlite3.Row) -> DailyStateRecord:
        return DailyStateRecord(
            id=r["id"], date=r["date"], side=r["side"],
            expiry=r["expiry"], strike=r["strike"], symbol=r["symbol"],
            p0=r["p0"], trigger=r["trigger"],
            reference_ts=r["reference_ts"],
            reference_latency_ms=r["reference_latency_ms"],
            status=r["status"], no_trade_reason=r["no_trade_reason"],
            extra=json.loads(r["extra"] or "{}"),
        )

    def now_ist_str(self) -> str:
        return datetime.now(IST).isoformat()
