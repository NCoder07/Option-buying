"""
paper_broker.py — PaperBroker: live market data, simulated fills.

Identical code path as live — uses real bid/ask from LiveDhanBroker for
fill simulation.  Orders are logged, never sent to exchange.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

import pytz

from .broker_port import (
    BrokerPort, ChainRow, Fill, OrderStatus, Position, Quote,
)
from .live_broker import LiveDhanBroker

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class PaperBroker(BrokerPort):
    """
    Wraps LiveDhanBroker for market data; simulates fills using live bid/ask.
    Buys fill at ask; sells fill at bid.
    All simulated orders are logged with spread information.
    """

    def __init__(self, live: LiveDhanBroker) -> None:
        self._live = live
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

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self._live.get_quote(symbol)

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
    # Simulated orders
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """Simulate a BUY: fill at live ask (or price if ask unavailable)."""
        quote = self._live.get_quote(symbol)
        fill_price = quote.ask if (quote and quote.ask > 0) else price
        spread = quote.ask - quote.bid if quote else 0.0

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        self._orders[order_id] = {
            "symbol": symbol,
            "side": "BUY",
            "qty": qty,
            "submitted_price": price,
            "fill_price": fill_price,
            "spread": spread,
            "status": OrderStatus.FILLED,
            "timestamp": datetime.now(IST).isoformat(),
            "tag": tag,
        }
        logger.info(
            "[PAPER] BUY  %s qty=%d submitted=%.2f fill=%.2f spread=%.2f tag=%s id=%s",
            symbol, qty, price, fill_price, spread, tag, order_id,
        )
        # Update paper positions
        pos = self._positions.get(symbol)
        if pos:
            # Average up
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
        """Simulate a SELL: fill at live bid (or price if bid unavailable)."""
        quote = self._live.get_quote(symbol)
        fill_price = quote.bid if (quote and quote.bid > 0) else price
        spread = quote.ask - quote.bid if quote else 0.0

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        self._orders[order_id] = {
            "symbol": symbol,
            "side": "SELL",
            "qty": qty,
            "submitted_price": price,
            "fill_price": fill_price,
            "spread": spread,
            "status": OrderStatus.FILLED,
            "timestamp": datetime.now(IST).isoformat(),
            "tag": tag,
        }
        logger.info(
            "[PAPER] SELL %s qty=%d submitted=%.2f fill=%.2f spread=%.2f tag=%s id=%s",
            symbol, qty, price, fill_price, spread, tag, order_id,
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
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())
