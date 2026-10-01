"""
journal.py — Trade journal and daily log writers.

Writes two CSV files:
  journal/trades.csv    — one row per completed trade
  journal/daily_log.csv — one row per day per side (including NO-TRADE days)

Also writes structured JSON-lines tick data to data/YYYY-MM-DD/.

All paths relative to the DATA_DIR passed at construction.
Headers are written only once (first row); subsequent runs append.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Trade journal schema (one row per trade, comparable to backtest trades.csv)
# ---------------------------------------------------------------------------

TRADE_FIELDS = [
    "date", "side", "expiry", "dte", "strike", "symbol",
    "p0", "trigger",
    "reference_ts", "reference_latency_ms",
    "cross_detected_time", "bid_at_cross", "ask_at_cross",
    "order_sent_time", "fill_time",
    "avg_fill_price",
    "entry_slippage_vs_ask", "entry_slippage_vs_trigger",
    "sl_price",
    "exit_reason",
    "exit_order_time", "exit_fill_time",
    "exit_fill_price",
    "exit_slippage_vs_bid", "exit_slippage_vs_sl",
    "gap_through_stop",
    "pnl_points", "pnl_inr",
    "lots", "lot_size", "qty",
    "flag_gap_cross", "flag_skipped_cap", "flag_late_entry",
    "flag_partial_fill", "flag_retries",
    "code_version", "config_hash",
    "mode",
]

DAILY_FIELDS = [
    "date", "side", "expiry", "status",
    "strike", "symbol", "p0", "trigger",
    "no_trade_reason",
    "pnl_points", "pnl_inr",
    "code_version", "config_hash",
    "mode",
]

TICK_FIELDS = [
    "ts", "symbol", "ltp", "bid", "ask", "bid_qty", "ask_qty",
    "event",
]


class Journal:
    def __init__(self, data_dir: Path, code_version: str, config_hash: str, mode: str) -> None:
        self._data_dir = data_dir
        self._code_version = code_version
        self._config_hash = config_hash
        self._mode = mode
        self._journal_dir = data_dir / "journal"
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        self._trades_path = self._journal_dir / "trades.csv"
        self._daily_path = self._journal_dir / "daily_log.csv"
        self._ensure_headers()
        self._tick_files: dict[str, Any] = {}   # symbol -> open file handle

    def _ensure_headers(self) -> None:
        if not self._trades_path.exists():
            with self._trades_path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=TRADE_FIELDS).writeheader()
        if not self._daily_path.exists():
            with self._daily_path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=DAILY_FIELDS).writeheader()

    # ------------------------------------------------------------------
    # Trade row
    # ------------------------------------------------------------------

    def write_trade(self, row: dict) -> None:
        row.setdefault("code_version", self._code_version)
        row.setdefault("config_hash", self._config_hash)
        row.setdefault("mode", self._mode)
        with self._trades_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            w.writerow(row)
        logger.info("JOURNAL trade: %s", {k: row.get(k) for k in
                    ["date", "side", "strike", "pnl_points", "exit_reason"]})

    # ------------------------------------------------------------------
    # Daily log row
    # ------------------------------------------------------------------

    def write_daily(self, row: dict) -> None:
        row.setdefault("code_version", self._code_version)
        row.setdefault("config_hash", self._config_hash)
        row.setdefault("mode", self._mode)
        with self._daily_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=DAILY_FIELDS, extrasaction="ignore")
            w.writerow(row)

    # ------------------------------------------------------------------
    # Tick data (JSON-lines, per day per symbol)
    # ------------------------------------------------------------------

    def _get_tick_file(self, date_str: str, symbol: str):
        key = f"{date_str}_{symbol}"
        if key not in self._tick_files:
            day_dir = self._data_dir / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            safe_sym = symbol.replace(" ", "_").replace("/", "_")
            path = day_dir / f"ticks_{safe_sym}.jsonl"
            self._tick_files[key] = path.open("a")
        return self._tick_files[key]

    def write_tick(
        self,
        date_str: str,
        symbol: str,
        ltp: float,
        bid: float,
        ask: float,
        event: str = "",
        bid_qty: int = 0,
        ask_qty: int = 0,
    ) -> None:
        record = {
            "ts": datetime.now(IST).isoformat(),
            "symbol": symbol,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "event": event,
        }
        try:
            f = self._get_tick_file(date_str, symbol)
            f.write(json.dumps(record) + "\n")
            f.flush()
        except Exception as e:
            logger.warning("write_tick failed: %s", e)

    # ------------------------------------------------------------------
    # Chain snapshot (09:20)
    # ------------------------------------------------------------------

    def write_chain_snapshot(self, date_str: str, rows: list, snapshot_ts: str) -> None:
        day_dir = self._data_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"chain_snapshot_{snapshot_ts.replace(':', '').replace('-', '')[:15]}.json"
        data = {
            "snapshot_ts": snapshot_ts,
            "date": date_str,
            "rows": [
                {
                    "strike": r.strike, "option_type": r.option_type,
                    "symbol": r.symbol, "ltp": r.ltp,
                    "bid": r.bid, "ask": r.ask, "oi": r.oi, "iv": r.iv,
                }
                for r in rows
            ],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Chain snapshot written: %s (%d rows)", path.name, len(rows))

    # ------------------------------------------------------------------
    # Order API log
    # ------------------------------------------------------------------

    def write_order_log(self, date_str: str, event: str, payload: dict) -> None:
        day_dir = self._data_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / "orders.jsonl"
        record = {"ts": datetime.now(IST).isoformat(), "event": event, **payload}
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def close(self) -> None:
        for f in self._tick_files.values():
            try:
                f.close()
            except Exception:
                pass
        self._tick_files.clear()


# ---------------------------------------------------------------------------
# P&L helper
# ---------------------------------------------------------------------------

def compute_pnl(
    avg_entry: float,
    avg_exit: float,
    qty: int,
    lot_size: int,
    lots: int,
) -> tuple[float, float]:
    """Return (pnl_points, pnl_inr)."""
    pnl_points = avg_exit - avg_entry
    pnl_inr = pnl_points * qty
    return round(pnl_points, 2), round(pnl_inr, 2)
