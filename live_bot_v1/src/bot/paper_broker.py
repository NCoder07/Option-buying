"""
paper_broker.py — PaperBroker: live market data, deterministic simulated fills.

Pricing model: everything off LTP.  No bid/ask.

Fill simulation:
  - BUY  fills at LTP + paper_slippage_points  (configurable; default 1.0)
  - SELL fills at LTP - paper_slippage_points

If no valid LTP is available at order time, the order is REJECTED (not filled
at a default price).  This is the explicit guard against the ₹0.50 fill bug
that occurred when the previous code fell back to the submitted price.

paper_slippage_points is a clearly-labelled assumption; it is recorded in
every journal row so forward-test results can be adjusted if the assumption
turns out to be wrong.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

import pytz

from .broker_port import (
    BrokerPort, ChainRow, Fill, LTPSnapshot, OrderStatus, Position,
)
from .live_broker import LiveDhanBroker

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_DEFAULT_SLIPPAGE = 1.0   # points per side (assumption; documented in journal)


class PaperBroker(BrokerPort):
    """
    Wraps LiveDhanBroker for market data; simulates fills using live LTP
    ± paper_slippage_points.

    All simulated orders are logged.  The fill price is never a submitted
    price or a default — it is always derived from a freshly-fetched LTP.
    If no valid LTP exists, the order is REJECTED.
    """

    def __init__(self, live: LiveDhanBroker, config: Optional[dict] = None) -> None:
        self._live = live
        self._slippage = float((config or {}).get("paper_slippage_points", _DEFAULT_SLIPPAGE))
        self._orders: dict[str, dict] = {}     # order_id -> order record
        self._positions: dict[str, Position] = {}  # symbol -> Position

    # ------------------------------------------------------------------
    # Delegation to live for market data
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._live.connect()

    def is_connected(self) -> bool:
        return self._live.is_connected()

    def reconnect_if_needed(self) -> None:
        self._live.reconnect_if_needed()

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return self._live.get_ltp(symbols)

    def get_ltp_single(self, symbol: str) -> Optional[LTPSnapshot]:
        return self._live.get_ltp_single(symbol)

    def get_option_chain_snapshot(
        self, expiry_date: str, num_strikes: int = 20
    ) -> list[ChainRow]:
        return self._live.get_option_chain_snapshot(expiry_date, num_strikes)

    def get_expiry_list(self) -> list[str]:
        return self._live.get_expiry_list()

    def get_available_balance(self) -> float:
        return self._live.get_available_balance()

    def get_lot_size(self, symbol: str) -> int:
        return self._live.get_lot_size(symbol)

    # ------------------------------------------------------------------
    # Simulated orders — LTP-based fills only
    # ------------------------------------------------------------------

    def _fetch_ltp_or_reject(self, symbol: str, side: str) -> Optional[float]:
        """
        Fetch current LTP.  Returns the ltp float, or None if unavailable.
        Logs the reason when returning None so callers can REJECT the order.
        """
        snap = self._live.get_ltp_single(symbol)
        if snap is None:
            logger.error(
                "[PAPER] %s %s — no_valid_price: LTP fetch returned None; order REJECTED",
                side, symbol,
            )
            return None
        if snap.ltp <= 0:
            logger.error(
                "[PAPER] %s %s — no_valid_price: LTP=%.4f <= 0; order REJECTED",
                side, symbol, snap.ltp,
            )
            return None
        return snap.ltp

    def place_limit_buy(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """
        Simulate a BUY.  Fill at LTP + paper_slippage_points.
        REJECTS (returns order_id with status REJECTED) if no valid LTP.
        """
        ltp = self._fetch_ltp_or_reject(symbol, "BUY")
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        if ltp is None:
            self._orders[order_id] = {
                "symbol": symbol, "side": "BUY", "qty": qty,
                "submitted_price": price, "fill_price": 0.0,
                "ltp_at_fill": None, "slippage_pts": self._slippage,
                "status": OrderStatus.REJECTED,
                "timestamp": datetime.now(IST).isoformat(),
                "tag": tag,
                "rejection_reason": "no_valid_price",
            }
            logger.error(
                "[PAPER] BUY  %s qty=%d REJECTED (no_valid_price) tag=%s id=%s",
                symbol, qty, tag, order_id,
            )
            return order_id

        fill_price = round(ltp + self._slippage, 2)
        self._orders[order_id] = {
            "symbol": symbol, "side": "BUY", "qty": qty,
            "submitted_price": price, "fill_price": fill_price,
            "ltp_at_fill": ltp, "slippage_pts": self._slippage,
            "status": OrderStatus.FILLED,
            "timestamp": datetime.now(IST).isoformat(),
            "tag": tag,
        }
        logger.info(
            "[PAPER] BUY  %s qty=%d ltp=%.2f slippage=%.2f fill=%.2f tag=%s id=%s",
            symbol, qty, ltp, self._slippage, fill_price, tag, order_id,
        )
        # Update paper positions
        pos = self._positions.get(symbol)
        if pos:
            total_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + fill_price * qty) / total_qty
            pos.qty = total_qty
        else:
            self._positions[symbol] = Position(
                symbol=symbol, qty=qty, avg_price=fill_price
            )
        return order_id

    def place_limit_sell(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """
        Simulate a SELL.  Fill at LTP - paper_slippage_points.
        REJECTS if no valid LTP.
        """
        ltp = self._fetch_ltp_or_reject(symbol, "SELL")
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        if ltp is None:
            self._orders[order_id] = {
                "symbol": symbol, "side": "SELL", "qty": qty,
                "submitted_price": price, "fill_price": 0.0,
                "ltp_at_fill": None, "slippage_pts": self._slippage,
                "status": OrderStatus.REJECTED,
                "timestamp": datetime.now(IST).isoformat(),
                "tag": tag,
                "rejection_reason": "no_valid_price",
            }
            logger.error(
                "[PAPER] SELL %s qty=%d REJECTED (no_valid_price) tag=%s id=%s",
                symbol, qty, tag, order_id,
            )
            return order_id

        fill_price = max(round(ltp - self._slippage, 2), 0.05)
        self._orders[order_id] = {
            "symbol": symbol, "side": "SELL", "qty": qty,
            "submitted_price": price, "fill_price": fill_price,
            "ltp_at_fill": ltp, "slippage_pts": self._slippage,
            "status": OrderStatus.FILLED,
            "timestamp": datetime.now(IST).isoformat(),
            "tag": tag,
        }
        logger.info(
            "[PAPER] SELL %s qty=%d ltp=%.2f slippage=%.2f fill=%.2f tag=%s id=%s",
            symbol, qty, ltp, self._slippage, fill_price, tag, order_id,
        )
        # Update paper positions
        pos = self._positions.get(symbol)
        if pos:
            pos.qty -= qty
            if pos.qty <= 0:
                del self._positions[symbol]
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = OrderStatus.CANCELLED
            logger.info("[PAPER] cancel_order(%s)", order_id)
            return True
        return False

    def get_order_status(self, order_id: str) -> Fill:
        rec = self._orders.get(order_id)
        if not rec:
            # If this is a paper order ID from a previous run (lost on restart),
            # return FILLED so reconcile() doesn't halt.  The fill price is unknown
            # but the position is already recorded in the DB with the correct avg_fill.
            if order_id.startswith("PAPER-"):
                logger.debug(
                    "get_order_status(%s): paper order from prior run — assuming FILLED",
                    order_id,
                )
                return Fill(
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    avg_price=0.0,   # unknown after restart; DB value is authoritative
                    filled_qty=0,
                    remaining_qty=0,
                )
            return Fill(
                order_id=order_id,
                status=OrderStatus.UNKNOWN,
                avg_price=0.0,
                filled_qty=0,
                remaining_qty=0,
            )
        status = rec["status"]
        qty = rec["qty"]
        filled = qty if status == OrderStatus.FILLED else 0
        return Fill(
            order_id=order_id,
            status=status,
            avg_price=rec["fill_price"] if status == OrderStatus.FILLED else 0.0,
            filled_qty=filled,
            remaining_qty=qty - filled,
            rejection_reason=rec.get("rejection_reason"),
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())
