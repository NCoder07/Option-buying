"""
test_reconcile.py — Tests for the lifecycle.reconcile() function.

Tests the overnight position recovery logic:
  - No open positions → passes cleanly
  - Open position found at broker → passes
  - Open position NOT at broker → marks DB closed + alerts (reconcile_missing)
  - Unknown order status → halts + returns False
  - VM reboot scenario: DB has open position, broker still shows it → passes
  - Crash mid-entry (position in DB but order status UNKNOWN) → halts
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from bot.broker_port import BrokerPort, ChainRow, Fill, OrderStatus, Position
from bot.lifecycle import reconcile
from bot.state_db import StateDB, PositionRecord


# ---------------------------------------------------------------------------
# Minimal stub broker
# ---------------------------------------------------------------------------

class StubBroker(BrokerPort):
    """Minimal stub — only implements what reconcile() calls."""

    def __init__(self, positions: list[Position], order_statuses: dict[str, OrderStatus]):
        self._positions = positions
        self._order_statuses = order_statuses

    def get_positions(self) -> list[Position]:
        return self._positions

    def get_order_status(self, order_id: str) -> Fill:
        status = self._order_statuses.get(order_id, OrderStatus.FILLED)
        return Fill(
            order_id=order_id,
            status=status,
            avg_price=65.0 if status == OrderStatus.FILLED else 0.0,
            filled_qty=75 if status == OrderStatus.FILLED else 0,
            remaining_qty=0,
        )

    # Unused stubs
    def get_ltp(self, symbols): return {}
    def get_ltp_single(self, symbol): return None
    def get_option_chain_snapshot(self, expiry_date, num_strikes=20): return []
    def get_expiry_list(self): return []
    def place_limit_buy(self, symbol, price, qty, tag=None): return None
    def place_limit_sell(self, symbol, price, qty, tag=None): return None
    def cancel_order(self, order_id): return False
    def get_available_balance(self): return 0.0
    def get_lot_size(self, symbol): return 75
    def connect(self): pass
    def is_connected(self): return True
    def reconnect_if_needed(self): pass


def _make_db_with_position(tmp_path: Path, symbol: str, order_id: str) -> StateDB:
    """Helper: create a fresh DB with one open position."""
    db = StateDB(tmp_path / "test.sqlite")
    pos = PositionRecord(
        date="2025-06-12",
        side="CE",
        symbol=symbol,
        expiry="2025-06-12",
        strike=25200,
        qty=75,
        avg_fill_price=65.0,
        sl_price=32.5,
        entry_order_id=order_id,
    )
    db.upsert_position(pos)
    return db


def _stub_alerts():
    """Return a mock Alerts-like object that records calls."""
    alerts = MagicMock()
    alerts._halted = False
    def halt(msg):
        alerts._halted = True
        alerts.halt_msg = msg
    alerts.halt.side_effect = halt
    return alerts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconcileNoPositions:
    def test_passes_when_db_is_empty(self, tmp_path):
        db = StateDB(tmp_path / "test.sqlite")
        broker = StubBroker(positions=[], order_statuses={})
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        assert result is True


class TestReconcilePositionAtBroker:
    SYM = "NIFTY 12JUN25 25200 CE"

    def test_open_position_found_at_broker_passes(self, tmp_path):
        """VM reboot scenario: DB has open position, broker confirms it exists."""
        db = _make_db_with_position(tmp_path, self.SYM, "ORD001")
        broker = StubBroker(
            positions=[Position(symbol=self.SYM, qty=75, avg_price=65.0)],
            order_statuses={"ORD001": OrderStatus.FILLED},
        )
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        assert result is True
        # Position should still be open in DB
        assert len(db.get_open_positions()) == 1

    def test_ce_and_pe_both_found_passes(self, tmp_path):
        """Both CE and PE overnight positions confirmed at broker."""
        db = StateDB(tmp_path / "test.sqlite")
        for side, sym, strike in [("CE", "NIFTY 12JUN25 25200 CE", 25200), ("PE", "NIFTY 12JUN25 24800 PE", 24800)]:
            pos = PositionRecord(
                date="2025-06-12", side=side, symbol=sym,
                expiry="2025-06-12", strike=strike,
                qty=75, avg_fill_price=65.0, sl_price=32.5,
                entry_order_id=f"ORD_{side}",
            )
            db.upsert_position(pos)
        broker = StubBroker(
            positions=[
                Position(symbol="NIFTY 12JUN25 25200 CE", qty=75, avg_price=65.0),
                Position(symbol="NIFTY 12JUN25 24800 PE", qty=75, avg_price=65.0),
            ],
            order_statuses={
                "ORD_CE": OrderStatus.FILLED,
                "ORD_PE": OrderStatus.FILLED,
            },
        )
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        assert result is True


class TestReconcilePositionMissingAtBroker:
    SYM = "NIFTY 12JUN25 25200 CE"

    def test_missing_position_marked_closed(self, tmp_path):
        """
        DB has open position but broker no longer shows it.
        Could happen if manually squared off or Dhan auto-squared-off (MIS error).
        Reconcile should mark it closed and alert, but still return True.
        """
        db = _make_db_with_position(tmp_path, self.SYM, "ORD001")
        # Broker shows NO positions
        broker = StubBroker(positions=[], order_statuses={"ORD001": OrderStatus.FILLED})
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        # Returns True (not a fatal halt — we just flag it)
        assert result is True
        # Position should now be CLOSED in DB
        open_pos = db.get_open_positions()
        assert len(open_pos) == 0
        # An alert must have been sent
        alerts.send.assert_called_once()
        call_args = alerts.send.call_args
        assert "reconcile_missing" in str(call_args) or "not found at broker" in str(call_args)

    def test_missing_position_exit_reason_set(self, tmp_path):
        """exit_reason must be 'reconcile_missing' after DB update."""
        db = _make_db_with_position(tmp_path, self.SYM, "ORD001")
        broker = StubBroker(positions=[], order_statuses={"ORD001": OrderStatus.FILLED})
        alerts = _stub_alerts()
        reconcile(broker, db, alerts, date(2025, 6, 12))
        # Verify via DB — get_open_positions returns only OPEN; check all
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "test.sqlite"))
        rows = conn.execute("SELECT status, exit_reason FROM positions").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "CLOSED"
        assert rows[0][1] == "reconcile_missing"


class TestReconcileUnknownOrderStatus:
    SYM = "NIFTY 12JUN25 25200 CE"

    def test_unknown_order_halts_and_returns_false(self, tmp_path):
        """
        If an open DB position's entry order comes back UNKNOWN (Dhan returned
        something we can't interpret), reconcile MUST halt new entries.
        This prevents the bot from trading with an unresolved order state.
        """
        db = _make_db_with_position(tmp_path, self.SYM, "ORD_MYSTERY")
        broker = StubBroker(
            positions=[Position(symbol=self.SYM, qty=75, avg_price=65.0)],
            order_statuses={"ORD_MYSTERY": OrderStatus.UNKNOWN},
        )
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        assert result is False
        alerts.halt.assert_called_once()

    def test_rejected_order_does_not_halt(self, tmp_path):
        """
        REJECTED is a known terminal state — reconcile must not halt for it.
        (The position may still need manual cleanup, but the bot can keep trading.)
        """
        db = _make_db_with_position(tmp_path, self.SYM, "ORD_REJECT")
        broker = StubBroker(
            positions=[Position(symbol=self.SYM, qty=75, avg_price=65.0)],
            order_statuses={"ORD_REJECT": OrderStatus.REJECTED},
        )
        alerts = _stub_alerts()
        result = reconcile(broker, db, alerts, date(2025, 6, 12))
        assert result is True
        alerts.halt.assert_not_called()


class TestReconcileOvernightReboot:
    """
    Simulate a VM reboot overnight with an open position.
    The bot restarts, calls morning_init() which calls reconcile().
    The broker still shows the position (it's a carry-forward MARGIN position).
    """
    SYM = "NIFTY 12JUN25 25200 CE"

    def test_reboot_with_open_position_recovers(self, tmp_path):
        """
        After VM reboot:
        1. DB on persistent disk still has the open position.
        2. Broker confirms the position exists.
        3. Reconcile returns True — bot is ready to manage the position.
        """
        # Simulate persistent state: write position on "prior" StateDB instance
        db_path = tmp_path / "state.sqlite"
        db1 = StateDB(db_path)
        pos = PositionRecord(
            date="2025-06-11",        # entered yesterday
            side="CE",
            symbol=self.SYM,
            expiry="2025-06-12",      # expiry tomorrow
            strike=25200,
            qty=75,
            avg_fill_price=65.0,
            sl_price=32.5,
            entry_order_id="ORD_OVERNIGHT",
        )
        db1.upsert_position(pos)
        del db1  # simulate reboot — old instance gone

        # New DB instance (as if bot just started)
        db2 = StateDB(db_path)
        assert len(db2.get_open_positions()) == 1   # survives reboot

        broker = StubBroker(
            positions=[Position(symbol=self.SYM, qty=75, avg_price=65.0)],
            order_statuses={"ORD_OVERNIGHT": OrderStatus.FILLED},
        )
        alerts = _stub_alerts()
        result = reconcile(broker, db2, alerts, date(2025, 6, 12))
        assert result is True
        # Position must still be OPEN — not removed
        assert len(db2.get_open_positions()) == 1

    def test_reboot_position_manually_closed_overnight(self, tmp_path):
        """
        After VM reboot, if position was manually squared off while bot was down,
        reconcile should detect it missing and mark DB closed.
        """
        db_path = tmp_path / "state.sqlite"
        db1 = StateDB(db_path)
        pos = PositionRecord(
            date="2025-06-11", side="CE", symbol=self.SYM,
            expiry="2025-06-12", strike=25200, qty=75,
            avg_fill_price=65.0, sl_price=32.5,
            entry_order_id="ORD_GONE",
        )
        db1.upsert_position(pos)

        db2 = StateDB(db_path)
        broker = StubBroker(positions=[], order_statuses={})  # position gone
        alerts = _stub_alerts()
        reconcile(broker, db2, alerts, date(2025, 6, 12))
        assert len(db2.get_open_positions()) == 0   # marked closed
        alerts.send.assert_called_once()
