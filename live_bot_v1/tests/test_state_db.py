"""
test_state_db.py — Tests for SQLite state persistence.

Covers:
  - Position CRUD and status transitions
  - Order idempotency (INSERT OR IGNORE)
  - Daily state get-or-create and update
  - Restart recovery (DB persists across StateDB instances)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bot.state_db import StateDB, PositionRecord, OrderRecord, DailyStateRecord


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.sqlite")
        yield db


class TestPositions:
    def test_insert_and_retrieve(self, tmp_db):
        pos = PositionRecord(
            date="2025-06-12", side="CE", symbol="NIFTY 12JUN25 25200 CE",
            expiry="2025-06-12", strike=25200, qty=75,
            avg_fill_price=65.0, sl_price=32.5,
            entry_order_id="ORD001",
        )
        pos_id = tmp_db.upsert_position(pos)
        assert pos_id > 0
        open_pos = tmp_db.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "NIFTY 12JUN25 25200 CE"
        assert open_pos[0].avg_fill_price == 65.0
        assert open_pos[0].sl_price == 32.5

    def test_update_position_status(self, tmp_db):
        pos = PositionRecord(
            date="2025-06-12", side="CE", symbol="NIFTY 12JUN25 25200 CE",
            expiry="2025-06-12", strike=25200, qty=75,
            avg_fill_price=65.0, sl_price=32.5,
            entry_order_id="ORD001",
        )
        pos.id = tmp_db.upsert_position(pos)
        pos.status = "CLOSED"
        pos.exit_reason = "sl_intraday"
        pos.exit_fill_price = 30.0
        tmp_db.upsert_position(pos)

        open_pos = tmp_db.get_open_positions()
        assert len(open_pos) == 0

    def test_multiple_sides(self, tmp_db):
        for side in ["CE", "PE"]:
            pos = PositionRecord(
                date="2025-06-12", side=side,
                symbol=f"NIFTY 12JUN25 25200 {side}",
                expiry="2025-06-12", strike=25200, qty=75,
                avg_fill_price=65.0, sl_price=32.5,
                entry_order_id=f"ORD_{side}",
            )
            tmp_db.upsert_position(pos)
        open_pos = tmp_db.get_open_positions()
        assert len(open_pos) == 2
        sides = {p.side for p in open_pos}
        assert sides == {"CE", "PE"}

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite"
            db1 = StateDB(db_path)
            pos = PositionRecord(
                date="2025-06-12", side="CE", symbol="SYM",
                expiry="2025-06-12", strike=25200, qty=75,
                avg_fill_price=65.0, sl_price=32.5,
                entry_order_id="ORD_X",
            )
            db1.upsert_position(pos)

            # New DB instance — same file
            db2 = StateDB(db_path)
            open_pos = db2.get_open_positions()
            assert len(open_pos) == 1


class TestOrders:
    def test_insert_order(self, tmp_db):
        rec = OrderRecord(
            order_id="ORD001", date="2025-06-12", side="CE",
            symbol="NIFTY 12JUN25 25200 CE", direction="BUY",
            qty=75, submitted_price=65.50, tag="ENTRY_CE",
        )
        tmp_db.insert_order(rec)
        assert tmp_db.order_exists("ORD001")

    def test_idempotent_insert(self, tmp_db):
        rec = OrderRecord(
            order_id="ORD001", date="2025-06-12", side="CE",
            symbol="NIFTY 12JUN25 25200 CE", direction="BUY",
            qty=75, submitted_price=65.50,
        )
        tmp_db.insert_order(rec)
        tmp_db.insert_order(rec)  # second insert is ignored
        orders = tmp_db.get_orders_for_date("2025-06-12")
        assert len(orders) == 1

    def test_order_not_exists(self, tmp_db):
        assert not tmp_db.order_exists("NONEXISTENT")


class TestDailyState:
    def test_get_or_create(self, tmp_db):
        ds = tmp_db.get_or_create_daily_state("2025-06-12", "CE")
        assert ds.date == "2025-06-12"
        assert ds.side == "CE"
        assert ds.status == "PENDING"

    def test_idempotent_create(self, tmp_db):
        ds1 = tmp_db.get_or_create_daily_state("2025-06-12", "CE")
        ds2 = tmp_db.get_or_create_daily_state("2025-06-12", "CE")
        assert ds1.status == ds2.status == "PENDING"

    def test_update_status(self, tmp_db):
        ds = tmp_db.get_or_create_daily_state("2025-06-12", "CE")
        ds.status = "SELECTED"
        ds.strike = 25200
        ds.symbol = "NIFTY 12JUN25 25200 CE"
        ds.p0 = 63.0
        ds.trigger = 94.5
        tmp_db.update_daily_state(ds)

        ds_retrieved = tmp_db.get_or_create_daily_state("2025-06-12", "CE")
        assert ds_retrieved.status == "SELECTED"
        assert ds_retrieved.strike == 25200
        assert ds_retrieved.p0 == 63.0
