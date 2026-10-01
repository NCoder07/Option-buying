"""
test_paper_broker.py — Tests for PaperBroker fill simulation.

Covers:
  - Buys fill at ask
  - Sells fill at bid
  - Position tracking
  - Order status query
  - Cancel
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from bot.broker_port import Quote, OrderStatus
from bot.paper_broker import PaperBroker


def _make_paper_broker(ask: float = 94.0, bid: float = 93.5):
    live = MagicMock()
    live.get_quote.return_value = Quote(
        symbol="NIFTY 12JUN25 25200 CE",
        ltp=93.75, bid=bid, ask=ask,
    )
    live.get_available_balance.return_value = 500_000.0
    live.get_lot_size.return_value = 75
    paper = PaperBroker(live)
    return paper


class TestPaperBrokerFills:
    def test_buy_fills_at_ask(self):
        paper = _make_paper_broker(ask=94.0)
        oid = paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 93.5, 75)
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.FILLED
        assert fill.avg_price == pytest.approx(94.0)  # filled at ask
        assert fill.filled_qty == 75

    def test_sell_fills_at_bid(self):
        paper = _make_paper_broker(bid=93.5)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 94.0, 75)
        oid = paper.place_limit_sell("NIFTY 12JUN25 25200 CE", 93.0, 75)
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.FILLED
        assert fill.avg_price == pytest.approx(93.5)  # filled at bid

    def test_position_tracked_after_buy(self):
        paper = _make_paper_broker(ask=94.0)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 93.5, 75)
        positions = paper.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 75
        assert positions[0].avg_price == pytest.approx(94.0)

    def test_position_cleared_after_full_sell(self):
        paper = _make_paper_broker(ask=94.0, bid=93.5)
        paper.place_limit_buy("NIFTY 12JUN25 25200 CE", 94.0, 75)
        paper.place_limit_sell("NIFTY 12JUN25 25200 CE", 93.0, 75)
        positions = paper.get_positions()
        assert len(positions) == 0

    def test_cancel_sets_cancelled(self):
        paper = _make_paper_broker()
        oid = paper.place_limit_buy("SYM", 94.0, 75)
        # Manually set to pending first (hack for test)
        paper._orders[oid]["status"] = OrderStatus.PENDING
        result = paper.cancel_order(oid)
        assert result is True
        fill = paper.get_order_status(oid)
        assert fill.status == OrderStatus.CANCELLED

    def test_unknown_order_returns_unknown_status(self):
        paper = _make_paper_broker()
        fill = paper.get_order_status("NONEXISTENT")
        assert fill.status == OrderStatus.UNKNOWN


class TestPaperBrokerPositionAveraging:
    def test_average_up_position(self):
        live = MagicMock()
        # First buy: ask=94.0, second buy: ask=96.0
        live.get_quote.side_effect = [
            Quote("SYM", ltp=93.75, bid=93.5, ask=94.0),
            Quote("SYM", ltp=95.75, bid=95.5, ask=96.0),
        ]
        paper = PaperBroker(live)
        paper.place_limit_buy("SYM", 93.5, 75)
        paper.place_limit_buy("SYM", 95.5, 75)
        positions = paper.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 150
        assert positions[0].avg_price == pytest.approx(95.0)  # (94*75 + 96*75) / 150
