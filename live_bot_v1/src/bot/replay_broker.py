"""
replay_broker.py — ReplayBroker: feeds 1-min historical candles as ticks.

Used on the dev machine for offline testing.  DATA_DIR is configurable
(set via BOT_DATA_DIR env var or --data-dir CLI flag); no /Users/... paths.

Expected data layout (same as Options Simulator):
  <DATA_DIR>/processed/<YYYY-MM-DD>_<symbol>.parquet
  OR
  <DATA_DIR>/<YYYY-MM-DD>/<symbol>.csv

The broker replays candles as if they arrived in real time.  Speed is
controlled by replay_speed config key (0.0 = as fast as possible, 1.0 = real).
"""

from __future__ import annotations

import csv
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

from .broker_port import (
    BrokerPort, ChainRow, Fill, OrderStatus, Position, Quote,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class ReplayBroker(BrokerPort):
    """
    Offline replay broker for backtesting and testing.

    Usage
    -----
        broker = ReplayBroker(data_dir=Path("/some/data"), config=cfg)
        broker.load_day(date(2024, 1, 15))
        # Then call broker.tick() in a loop to advance the replay clock
        # broker.now_ist() returns the current replay timestamp
    """

    def __init__(self, data_dir: Path, config: dict) -> None:
        self._data_dir = data_dir
        self._config = config
        self._replay_speed = float(config.get("replay_speed", 0.0))
        self._tick_frames: dict[str, pd.DataFrame] = {}   # symbol -> sorted candles
        self._current_idx: int = 0
        self._current_time: Optional[datetime] = None
        self._tick_data_for_time: dict[str, dict] = {}     # symbol -> latest row
        self._expiry_list: list[str] = []
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, dict] = {}
        self._lot_sizes: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Day setup
    # ------------------------------------------------------------------

    def load_day(self, replay_date: date, symbols: list[str]) -> None:
        """Load 1-min candle data for the given date and symbols."""
        self._tick_frames = {}
        self._current_idx = 0
        self._current_time = None
        for sym in symbols:
            df = self._load_candles(replay_date, sym)
            if df is not None and not df.empty:
                self._tick_frames[sym] = df
                logger.info("ReplayBroker: loaded %d candles for %s on %s", len(df), sym, replay_date)
            else:
                logger.warning("ReplayBroker: no candle data for %s on %s", sym, replay_date)

    def _load_candles(self, d: date, symbol: str) -> Optional[pd.DataFrame]:
        """Try several file path patterns used by the Options Simulator."""
        patterns = [
            self._data_dir / "processed" / f"{d}_{symbol}.parquet",
            self._data_dir / f"{d}" / f"{symbol}.parquet",
            self._data_dir / "processed" / f"{d}_{symbol}.csv",
            self._data_dir / f"{d}" / f"{symbol}.csv",
        ]
        for p in patterns:
            if p.exists():
                if p.suffix == ".parquet":
                    df = pd.read_parquet(p)
                else:
                    df = pd.read_csv(p)
                # Normalise timestamp column to tz-aware IST
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
                    if df["timestamp"].dt.tz is None:
                        df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
                    else:
                        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
                    df = df.sort_values("timestamp").reset_index(drop=True)
                return df
        return None

    def set_expiry_list(self, expiries: list[str]) -> None:
        """Set the expiry list manually for replay (no live API call)."""
        self._expiry_list = sorted(expiries)

    def set_lot_size(self, symbol: str, size: int) -> None:
        self._lot_sizes[symbol] = size

    def now_ist(self) -> Optional[datetime]:
        return self._current_time

    def tick(self) -> bool:
        """
        Advance one candle.  Returns True if more candles remain, False at EOD.
        Sets self._current_time and updates self._tick_data_for_time.
        """
        if not self._tick_frames:
            return False
        # Find the smallest next timestamp across all loaded symbols
        next_times = []
        for sym, df in self._tick_frames.items():
            idx_key = f"_idx_{sym}"
            idx = getattr(self, idx_key, 0)
            if idx < len(df):
                next_times.append((df.iloc[idx]["timestamp"], sym))
        if not next_times:
            return False
        next_times.sort(key=lambda x: x[0])
        next_ts, _ = next_times[0]

        # Advance all symbols that share this timestamp
        for sym, df in self._tick_frames.items():
            idx_key = f"_idx_{sym}"
            idx = getattr(self, idx_key, 0)
            if idx < len(df) and df.iloc[idx]["timestamp"] == next_ts:
                self._tick_data_for_time[sym] = df.iloc[idx].to_dict()
                setattr(self, idx_key, idx + 1)

        if self._replay_speed > 0.0 and self._current_time is not None:
            delta = (next_ts - self._current_time).total_seconds()
            time.sleep(delta / self._replay_speed)

        self._current_time = next_ts
        return True

    # ------------------------------------------------------------------
    # BrokerPort interface
    # ------------------------------------------------------------------

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        result = {}
        for sym in symbols:
            row = self._tick_data_for_time.get(sym)
            if row is not None:
                result[sym] = float(row.get("close", row.get("ltp", 0)))
        return result

    def get_quote(self, symbol: str) -> Optional[Quote]:
        row = self._tick_data_for_time.get(symbol)
        if row is None:
            return None
        ltp = float(row.get("close", row.get("ltp", 0)))
        # Simulate bid/ask with a small spread if not in data
        bid = float(row.get("bid", ltp - 0.05))
        ask = float(row.get("ask", ltp + 0.05))
        return Quote(
            symbol=symbol,
            ltp=ltp,
            bid=bid,
            ask=ask,
            timestamp=self._current_time,
        )

    def get_option_chain_snapshot(
        self, expiry_date: str, num_strikes: int = 20
    ) -> list[ChainRow]:
        """
        For replay, the chain snapshot is built from whatever tick data
        is available at the current replay time.  If the full chain is not
        loaded, returns only the symbols that are.
        """
        rows = []
        for sym, row in self._tick_data_for_time.items():
            if "CE" in sym or "PE" in sym:
                ltp = float(row.get("close", row.get("ltp", 0)))
                bid = float(row.get("bid", ltp - 0.05))
                ask = float(row.get("ask", ltp + 0.05))
                # Parse strike from symbol name (best-effort)
                try:
                    parts = sym.split()
                    strike = int(parts[-2]) if len(parts) >= 3 else 0
                    opt_type = parts[-1]
                except Exception:
                    strike = 0
                    opt_type = "CE" if "CE" in sym else "PE"
                rows.append(ChainRow(
                    strike=strike, option_type=opt_type, symbol=sym,
                    ltp=ltp, bid=bid, ask=ask,
                ))
        return rows

    def get_expiry_list(self) -> list[str]:
        return self._expiry_list

    def place_limit_buy(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        quote = self.get_quote(symbol)
        fill_price = quote.ask if (quote and quote.ask > 0) else price
        order_id = f"REPLAY-{uuid.uuid4().hex[:8].upper()}"
        self._orders[order_id] = {
            "symbol": symbol, "side": "BUY", "qty": qty,
            "fill_price": fill_price, "status": OrderStatus.FILLED,
        }
        pos = self._positions.get(symbol)
        if pos:
            total = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + fill_price * qty) / total
            pos.qty = total
        else:
            self._positions[symbol] = Position(symbol=symbol, qty=qty, avg_price=fill_price)
        logger.debug("[REPLAY] BUY %s qty=%d @ %.2f", symbol, qty, fill_price)
        return order_id

    def place_limit_sell(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        quote = self.get_quote(symbol)
        fill_price = quote.bid if (quote and quote.bid > 0) else price
        order_id = f"REPLAY-{uuid.uuid4().hex[:8].upper()}"
        self._orders[order_id] = {
            "symbol": symbol, "side": "SELL", "qty": qty,
            "fill_price": fill_price, "status": OrderStatus.FILLED,
        }
        pos = self._positions.get(symbol)
        if pos:
            pos.qty -= qty
            if pos.qty <= 0:
                del self._positions[symbol]
        logger.debug("[REPLAY] SELL %s qty=%d @ %.2f", symbol, qty, fill_price)
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = OrderStatus.CANCELLED
            return True
        return False

    def get_order_status(self, order_id: str) -> Fill:
        rec = self._orders.get(order_id)
        if not rec:
            return Fill(order_id=order_id, status=OrderStatus.UNKNOWN,
                        avg_price=0.0, filled_qty=0, remaining_qty=0)
        qty = rec["qty"]
        status = rec["status"]
        filled = qty if status == OrderStatus.FILLED else 0
        return Fill(
            order_id=order_id, status=status,
            avg_price=rec.get("fill_price", 0.0),
            filled_qty=filled, remaining_qty=qty - filled,
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_available_balance(self) -> float:
        return 1_000_000.0   # Simulated balance for replay

    def get_lot_size(self, symbol: str) -> int:
        return self._lot_sizes.get(symbol, 75)
