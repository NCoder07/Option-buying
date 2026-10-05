"""
test_paper_broker.py — Tests for PaperBroker LTP-based fill simulation.

Covers:
  - Buys fill at LTP + paper_slippage_points
  - Sells fill at LTP - paper_slippage_points
  - Position tracking
  - Order status query
  - Cancel
  - REGRESSION: no valid LTP → order REJECTED, never filled
    (This test reproduces the ₹0.50 fill bug: previously PaperBroker fell
     back to the submitted price when no live quote was available, resulting
     in trades like fill=₹0.50 against an ₹89.55 trigger and ₹93.50 LTP.)
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from bot.broker_port import LTPSnapshot, OrderStatus
from bot.paper_broker import PaperBroker

import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")


def _ts() -> datetime:
    return datetime.now(IST)


def _make_paper_broker(ltp: float = 93.75, slippage: float = 1.0):
    live = MagicMock()
    live.get_ltp_single.return_value = LTPSnapshot(
        symbol="NIFTY 12JUN25 25200 CE",
        ltp=ltp,
        timestamp=_ts(),
    )
    live.get_available_balance.return_value = 500_000.0
    live.get_lot_size.return_value = 75
    config = {"paper_slippage_points": slippage}
    paper = PaperBroker(live, config=config)
    return paper


class TestPaperBrokerFills:
    def test_buy_fills_at_ltp_plus_slippage(self):
        paper = _make_paper_broker(ltp=93.75, slippage=1.0)
        oid = paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 94.0, 75)
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.FILLED
        assert fill.avg_price == pytest.approx(94.75)  # 93.75 + 1.0
        assert fill.filled_qty == 75

    def test_sell_fills_at_ltp_minus_slippage(self):
        paper = _make_paper_broker(ltp=93.75, slippage=1.0)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 94.0, 75)
        oid = paper.place_limit_sell("NIFTY 12JUN25 25200 CE", 92.0, 75)
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.FILLED
        assert fill.avg_price == pytest.approx(92.75)  # 93.75 - 1.0

    def test_position_tracked_after_buy(self):
        paper = _make_paper_broker(ltp=93.75, slippage=1.0)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 93.5, 75)
        positions = paper.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 75
        assert positions[0].avg_price == pytest.approx(94.75)

    def test_position_cleared_after_full_sell(self):
        paper = _make_paper_broker(ltp=93.75, slippage=1.0)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 94.0, 75)
        paper.place_limit_sell("NIFTY 12JUN25 25200 CE", 93.0, 75)
        positions = paper.get_positions()
        assert len(positions) == 0

    def test_cancel_sets_cancelled(self):
        paper = _make_paper_broker()
        oid = paper.place_limit_buy("SYM", 94.0, 75)
        paper._orders[oid]["status"] = OrderStatus.PENDING
        result = paper.cancel_order(oid)
        assert result is True
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.CANCELLED

    def test_unknown_order_returns_unknown_status(self):
        paper = _make_paper_broker()
        fill = paper.get_order_status("NONEXISTENT")
        assert fill.status == OrderStatus.UNKNOWN


class TestPaperBrokerNoValidLTP:
    """
    REGRESSION: Reproduces the ₹0.50 fill bug.

    Previously, when no live quote was available, PaperBroker fell back to
    the submitted price (entry_limit_buffer = 0.50) as the fill price.
    This caused trades like:
      trigger=89.55, LTP=93.50, fill=₹0.50, SL=₹0.25
    which are obviously invalid.

    Now: if get_ltp_single returns None, the order must be REJECTED.
    """

    def test_buy_rejected_when_no_ltp(self):
        live = MagicMock()
        live.get_ltp_single.return_value = None   # no live price
        paper = PaperBroker(live, config={"paper_slippage_points": 1.0})

        oid = paper.place_limit_buy("NIFTY 06 OCT 22500 PUT", 0.50, 65, tag="ENTRY_PE")
        fill = paper.get_order_status(oid)

        # Must be REJECTED — never filled at ₹0.50 or any default
        assert fill.status == OrderStatus.REJECTED, (
            f"Expected REJECTED, got {fill.status}. "
            "This reproduces the ₹0.50 fill bug: no valid LTP must not produce a fill."
        )
        assert fill.avg_price == 0.0
        assert fill.filled_qty == 0
        assert fill.rejection_reason == "no_valid_price"
        # Position must NOT have been created
        assert len(paper.get_positions()) == 0

    def test_sell_rejected_when_no_ltp(self):
        live = MagicMock()
        live.get_ltp_single.return_value = None
        paper = PaperBroker(live, config={"paper_slippage_points": 1.0})

        oid = paper.place_limit_sell("NIFTY 06 OCT 22500 PUT", 0.25, 65, tag="EXIT_PE_sl")
        fill = paper.get_order_status(oid)

        assert fill.status == OrderStatus.REJECTED
        assert fill.avg_price == 0.0
        assert fill.filled_qty == 0
        assert fill.rejection_reason == "no_valid_price"

    def test_buy_rejected_when_ltp_is_zero(self):
        live = MagicMock()
        live.get_ltp_single.return_value = LTPSnapshot(
            symbol="NIFTY 06 OCT 22500 PUT", ltp=0.0, timestamp=_ts()
        )
        paper = PaperBroker(live, config={"paper_slippage_points": 1.0})

        oid = paper.place_limit_buy("NIFTY 06 OCT 22500 PUT", 0.50, 65)
        fill = paper.get_order_status(oid)

        assert fill.status == OrderStatus.REJECTED
        assert fill.filled_qty == 0

    def test_fill_price_is_never_submitted_price(self):
        """
        Submitted price must NEVER be used as fill price.
        Fill must always come from LTP ± slippage.
        """
        live = MagicMock()
        live.get_ltp_single.return_value = LTPSnapshot(
            symbol="SYM", ltp=93.50, timestamp=_ts()
        )
        paper = PaperBroker(live, config={"paper_slippage_points": 1.0})

        # Submit at ₹0.50 (the old broken value) — should NOT be the fill
        oid = paper.place_limit_buy("SYM", 0.50, 65)
        fill = paper.get_order_status(oid)

        assert fill.status == OrderStatus.FILLED
        assert fill.avg_price == pytest.approx(94.50)   # 93.50 LTP + 1.0 slippage
        assert fill.avg_price != pytest.approx(0.50)    # NEVER the submitted price


class TestPaperBrokerPositionAveraging:
    def test_average_up_position(self):
        live = MagicMock()
        live.get_ltp_single.side_effect = [
            LTPSnapshot("SYM", ltp=93.75, timestamp=_ts()),
            LTPSnapshot("SYM", ltp=95.75, timestamp=_ts()),
        ]
        paper = PaperBroker(live, config={"paper_slippage_points": 1.0})
        paper.place_limit_buy("SYM", 93.5, 75)
        paper.place_limit_buy("SYM", 95.5, 75)
        positions = paper.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 150
        # (93.75+1.0)*75 + (95.75+1.0)*75 / 150 = (94.75 + 96.75) / 2 = 95.75
        assert positions[0].avg_price == pytest.approx(95.75)
