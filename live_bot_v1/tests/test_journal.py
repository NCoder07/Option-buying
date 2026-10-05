"""
test_journal.py — Tests for journal and daily log writers.
"""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

import pytest

from bot.journal import Journal, compute_pnl


@pytest.fixture
def tmp_journal():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp), code_version="abc1234", config_hash="d3f456", mode="paper")
        try:
            yield j, Path(tmp)
        finally:
            j.close()   # flush + close open .jsonl handles before Windows tmpdir cleanup


class TestJournal:
    def test_trade_row_written(self, tmp_journal):
        j, data_dir = tmp_journal
        j.write_trade({
            "date": "2025-06-12", "side": "CE", "expiry": "2025-06-12",
            "dte": 0, "strike": 25200, "symbol": "NIFTY 12JUN25 25200 CE",
            "p0": 63.0, "trigger": 94.5, "avg_fill_price": 65.0,
            "sl_price": 32.5, "exit_reason": "sl_intraday",
            "exit_fill_price": 30.0, "pnl_points": -35.0, "pnl_inr": -2625.0,
        })
        trades = list(csv.DictReader((data_dir / "journal" / "trades.csv").open()))
        assert len(trades) == 1
        assert trades[0]["side"] == "CE"
        assert trades[0]["code_version"] == "abc1234"
        assert trades[0]["mode"] == "paper"

    def test_daily_row_written(self, tmp_journal):
        j, data_dir = tmp_journal
        j.write_daily({
            "date": "2025-06-12", "side": "PE",
            "expiry": "2025-06-12", "status": "NO_TRADE",
            "no_trade_reason": "no_eligible_strike",
        })
        rows = list(csv.DictReader((data_dir / "journal" / "daily_log.csv").open()))
        assert len(rows) == 1
        assert rows[0]["no_trade_reason"] == "no_eligible_strike"

    def test_tick_data_written(self, tmp_journal):
        j, data_dir = tmp_journal
        j.write_tick("2025-06-12", "NIFTY 12JUN25 25200 CE",
                     ltp=65.0, event="cross_monitor")
        tick_file = data_dir / "2025-06-12" / "ticks_NIFTY_12JUN25_25200_CE.jsonl"
        assert tick_file.exists()
        record = json.loads(tick_file.read_text().strip())
        assert record["ltp"] == 65.0
        assert record["event"] == "cross_monitor"
        assert "bid" not in record
        assert "ask" not in record

    def test_chain_snapshot_written(self, tmp_journal):
        j, data_dir = tmp_journal
        from bot.broker_port import ChainRow
        rows = [ChainRow(25200, "CE", "SYM", 65.0)]
        j.write_chain_snapshot("2025-06-12", rows, "2025-06-12T09:20:00+05:30")
        snapshots = list((data_dir / "2025-06-12").glob("chain_snapshot_*.json"))
        assert len(snapshots) == 1
        data = json.loads(snapshots[0].read_text())
        assert data["rows"][0]["ltp"] == 65.0

    def test_headers_written_once(self, tmp_journal):
        j, data_dir = tmp_journal
        # Write two trade rows
        for _ in range(2):
            j.write_trade({"date": "2025-06-12", "side": "CE"})
        rows = list(csv.DictReader((data_dir / "journal" / "trades.csv").open()))
        assert len(rows) == 2  # not 3 (header not doubled)


class TestComputePnL:
    def test_positive_pnl(self):
        pts, inr = compute_pnl(65.0, 95.0, 75, 75, 1)
        assert pts == pytest.approx(30.0)
        assert inr == pytest.approx(2250.0)

    def test_negative_pnl(self):
        pts, inr = compute_pnl(65.0, 30.0, 75, 75, 1)
        assert pts == pytest.approx(-35.0)
        assert inr == pytest.approx(-2625.0)
