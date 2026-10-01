"""
broker_port.py — Abstract BrokerPort interface.

All strategy logic talks ONLY to BrokerPort.  The three concrete
implementations (LiveDhanBroker, PaperBroker, ReplayBroker) plug in here.
No Dhan_Tradehull imports anywhere except live_broker.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


class Side(str, Enum):
    CE = "CE"
    PE = "PE"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"          # sent to exchange, not yet confirmed
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Quote:
    """Snapshot of a single option contract's market data."""
    symbol: str
    ltp: float
    bid: float
    ask: float
    bid_qty: int = 0
    ask_qty: int = 0
    timestamp: Optional[datetime] = None     # tz-aware IST
    oi: Optional[float] = None


@dataclass
class Fill:
    """Result of a completed or partial order."""
    order_id: str
    status: OrderStatus
    avg_price: float                         # 0.0 if not filled
    filled_qty: int                          # 0 if not filled
    remaining_qty: int
    exchange_time: Optional[str] = None      # raw exchange timestamp string
    rejection_reason: Optional[str] = None


@dataclass
class Position:
    symbol: str
    qty: int                                 # positive = long
    avg_price: float
    product_type: str = "MARGIN"


@dataclass
class ChainRow:
    """One row from the 09:20 candidate snapshot."""
    strike: int
    option_type: str                         # "CE" or "PE"
    symbol: str                              # trading symbol
    ltp: float
    bid: float
    ask: float
    oi: Optional[float] = None
    iv: Optional[float] = None


class BrokerPort(ABC):
    """Abstract broker interface — strategy never touches Dhan SDK directly."""

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    @abstractmethod
    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """Return {symbol: ltp} for each symbol.  Returns {} on failure."""
        ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Optional[Quote]:
        """Return full quote (ltp, bid, ask) for one symbol.  None on failure."""
        ...

    @abstractmethod
    def get_option_chain_snapshot(
        self,
        expiry_date: str,    # YYYY-MM-DD
        num_strikes: int = 20,
    ) -> list[ChainRow]:
        """Return CE+PE rows for the given expiry.  Empty list on failure."""
        ...

    @abstractmethod
    def get_expiry_list(self) -> list[str]:
        """Return sorted list of NIFTY weekly expiry dates as YYYY-MM-DD strings."""
        ...

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    @abstractmethod
    def place_limit_buy(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """Send marketable LIMIT BUY.  Returns order_id or None on failure."""
        ...

    @abstractmethod
    def place_limit_sell(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        """Send marketable LIMIT SELL (exit / SL).  Returns order_id or None."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.  Returns True if cancel was accepted."""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> Fill:
        """Poll order status and fill details."""
        ...

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return current open positions."""
        ...

    @abstractmethod
    def get_available_balance(self) -> float:
        """Return available cash/margin in ₹."""
        ...

    # ------------------------------------------------------------------
    # Lot size (from instrument master)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_lot_size(self, symbol: str) -> int:
        """Return lot size for the given option trading symbol."""
        ...
