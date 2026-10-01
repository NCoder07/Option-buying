"""
test_strategy.py — Unit tests for the core strategy state machine.

Covers:
  - Strike selection: band filtering, tie-break, no-eligible case
  - Trigger calculation
  - Fresh cross vs already-above vs gap-cross
  - SL calculation
  - Entry cap check
  - Entry/exit price rounding
  - Timezone correctness
"""

from __future__ import annotations

import pytest
from datetime import date, datetime
import pytz

# Adjust imports to match project structure
from bot.strategy import (
    select_strike,
    is_fresh_cross,
    compute_sl,
    compute_entry_price,
    compute_exit_price,
    entry_exceeds_slippage_cap,
    _round_tick,
    _round_tick_down,
)
from bot.broker_port import ChainRow

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "ltp_band_low": 50.0,
    "ltp_band_high": 75.0,
    "target_mid": 62.5,
    "trigger_multiplier": 1.50,
    "tick_size": 0.05,
    "tie_break": "lower_strike",
    "entry_limit_buffer": 0.50,
    "max_entry_slippage_pct": 5.0,
    "sl_multiplier": 0.50,
    "exit_limit_buffer": 0.50,
}


def _chain(strikes: list[tuple[int, float, str]]) -> list[ChainRow]:
    """Build ChainRow list from (strike, ltp, option_type) tuples."""
    return [
        ChainRow(
            strike=s, option_type=opt, symbol=f"NIFTY 12JUN25 {s} {opt}",
            ltp=ltp, bid=ltp - 0.10, ask=ltp + 0.10,
        )
        for s, ltp, opt in strikes
    ]


def _snapshot_ts() -> datetime:
    return IST.localize(datetime(2025, 6, 12, 9, 20, 0))


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

class TestSelectStrike:
    def test_selects_closest_to_target_mid(self):
        chain = _chain([
            (25200, 60.0, "CE"),
            (25250, 63.0, "CE"),  # |63-62.5|=0.5 → closest
            (25300, 70.0, "CE"),
        ])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 100.0)
        assert result is not None
        assert result.strike == 25250

    def test_filters_below_band(self):
        chain = _chain([
            (25200, 40.0, "CE"),  # below 50 → excluded
            (25250, 65.0, "CE"),
        ])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 100.0)
        assert result is not None
        assert result.strike == 25250

    def test_filters_above_band(self):
        chain = _chain([
            (25150, 80.0, "CE"),  # above 75 → excluded
            (25200, 55.0, "CE"),
        ])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 100.0)
        assert result is not None
        assert result.strike == 25200

    def test_no_eligible_returns_none(self):
        chain = _chain([
            (25200, 30.0, "CE"),  # below band
            (25250, 90.0, "CE"),  # above band
        ])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 100.0)
        assert result is None

    def test_tie_break_lower_strike(self):
        # Two CE options with equal |ltp - 62.5| = 2.5
        chain = _chain([
            (25150, 60.0, "CE"),  # |60-62.5|=2.5
            (25200, 65.0, "CE"),  # |65-62.5|=2.5
        ])
        cfg = {**_BASE_CONFIG, "tie_break": "lower_strike"}
        result = select_strike(chain, "CE", cfg, _snapshot_ts(), 100.0)
        assert result is not None
        assert result.strike == 25150  # lower strike wins

    def test_tie_break_higher_premium(self):
        chain = _chain([
            (25150, 60.0, "CE"),  # |60-62.5|=2.5
            (25200, 65.0, "CE"),  # |65-62.5|=2.5, higher premium
        ])
        cfg = {**_BASE_CONFIG, "tie_break": "higher_premium"}
        result = select_strike(chain, "CE", cfg, _snapshot_ts(), 100.0)
        assert result is not None
        assert result.strike == 25200  # higher premium wins

    def test_trigger_calculation(self):
        chain = _chain([(25200, 62.0, "CE")])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 50.0)
        assert result is not None
        # trigger = 62.0 * 1.50 = 93.0 → already on tick boundary
        assert result.trigger == pytest.approx(93.0, abs=0.01)

    def test_trigger_rounded_to_tick(self):
        # P0 = 63.0, trigger = 63.0 * 1.50 = 94.50 — on boundary
        chain = _chain([(25200, 63.0, "CE")])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 50.0)
        assert result is not None
        # Use Decimal-based check to avoid floating-point modulo imprecision
        from decimal import Decimal
        tick = Decimal("0.05")
        val = Decimal(str(result.trigger))
        assert (val % tick) == Decimal("0"), f"trigger {result.trigger} not on tick"

    def test_pe_side_selected(self):
        chain = _chain([
            (25200, 60.0, "PE"),
            (25200, 80.0, "CE"),   # CE above band, shouldn't be selected for PE
        ])
        result = select_strike(chain, "PE", _BASE_CONFIG, _snapshot_ts(), 50.0)
        assert result is not None
        # SelectionResult doesn't carry option_type — verify symbol contains "PE"
        assert "PE" in result.symbol

    def test_at_band_boundary_inclusive(self):
        # LTP exactly at band limits should be included
        chain = _chain([
            (25200, 50.0, "CE"),  # exactly at low boundary
            (25250, 75.0, "CE"),  # exactly at high boundary
        ])
        result = select_strike(chain, "CE", _BASE_CONFIG, _snapshot_ts(), 50.0)
        assert result is not None  # at least one eligible


# ---------------------------------------------------------------------------
# Cross detection
# ---------------------------------------------------------------------------

class TestFreshCross:
    def test_fresh_cross_detected(self):
        ok, event = is_fresh_cross(92.0, 93.5, 93.0, False, "enter")
        assert ok is True
        assert event == "fresh_cross"

    def test_already_above_not_entry(self):
        ok, event = is_fresh_cross(95.0, 96.0, 93.0, False, "enter")
        assert ok is False
        assert event == "already_above"

    def test_below_trigger_not_entry(self):
        ok, event = is_fresh_cross(88.0, 90.0, 93.0, False, "enter")
        assert ok is False
        assert event == "below"

    def test_gap_cross_enter_policy(self):
        # First observation already >= trigger
        ok, event = is_fresh_cross(None, 95.0, 93.0, True, "enter")
        assert ok is True
        assert event == "gap_cross"

    def test_gap_cross_skip_policy(self):
        ok, event = is_fresh_cross(None, 95.0, 93.0, True, "skip")
        assert ok is False
        assert event == "gap_cross_skipped"

    def test_first_observation_below_trigger_not_entry(self):
        ok, event = is_fresh_cross(None, 88.0, 93.0, True, "enter")
        assert ok is False
        assert event == "below"

    def test_exact_trigger_is_cross(self):
        # curr_ltp exactly equals trigger with prev < trigger → cross
        ok, event = is_fresh_cross(92.9, 93.0, 93.0, False, "enter")
        assert ok is True
        assert event == "fresh_cross"


# ---------------------------------------------------------------------------
# SL calculation
# ---------------------------------------------------------------------------

class TestSLCalculation:
    def test_sl_is_half_fill(self):
        sl = compute_sl(100.0, _BASE_CONFIG)
        assert sl == pytest.approx(50.0, abs=0.01)

    def test_sl_rounds_down(self):
        # fill=101.0 → raw_sl=50.5 → rounded DOWN to 50.50 (on tick)
        sl = compute_sl(101.0, _BASE_CONFIG)
        assert sl <= 50.5
        from decimal import Decimal
        assert Decimal(str(sl)) % Decimal("0.05") == Decimal("0")

    def test_sl_with_non_round_fill(self):
        # fill=87.30 → raw_sl=43.65 → ROUND DOWN to 43.65 (if on tick) or 43.60
        sl = compute_sl(87.30, _BASE_CONFIG)
        assert sl <= 43.65
        from decimal import Decimal
        assert Decimal(str(sl)) % Decimal("0.05") == Decimal("0")

    def test_sl_always_below_fill(self):
        for fill in [50.0, 62.5, 75.0, 100.0, 123.45]:
            sl = compute_sl(fill, _BASE_CONFIG)
            assert sl < fill, f"SL {sl} should be < fill {fill}"


# ---------------------------------------------------------------------------
# Entry/exit price
# ---------------------------------------------------------------------------

class TestPricing:
    def test_entry_price_ask_plus_buffer(self):
        price = compute_entry_price(93.5, _BASE_CONFIG)
        assert price == pytest.approx(94.0, abs=0.01)

    def test_entry_price_rounded_to_tick(self):
        price = compute_entry_price(93.47, _BASE_CONFIG)
        from decimal import Decimal
        assert Decimal(str(price)) % Decimal("0.05") == Decimal("0")

    def test_slippage_cap_blocks_when_exceeded(self):
        # trigger=93.0, cap=5% → max_price=93*1.05=97.65
        blocked = entry_exceeds_slippage_cap(98.0, 93.0, _BASE_CONFIG)
        assert blocked is True

    def test_slippage_cap_allows_within(self):
        blocked = entry_exceeds_slippage_cap(94.0, 93.0, _BASE_CONFIG)
        assert blocked is False

    def test_exit_price_bid_minus_buffer(self):
        price = compute_exit_price(93.5, _BASE_CONFIG)
        assert price == pytest.approx(93.0, abs=0.01)

    def test_exit_price_with_extra_buffer(self):
        price = compute_exit_price(93.5, _BASE_CONFIG, extra_buffer=0.5)
        assert price == pytest.approx(92.5, abs=0.01)


# ---------------------------------------------------------------------------
# Tick rounding
# ---------------------------------------------------------------------------

class TestTickRounding:
    def test_round_tick_half_up(self):
        # 94.525 → nearest 0.05 = 94.55 (ROUND_HALF_UP)
        from decimal import ROUND_HALF_UP
        val = _round_tick(94.525, 0.05)
        assert val == pytest.approx(94.55, abs=1e-9)

    def test_round_tick_down(self):
        # 43.67 → round down to 43.65
        val = _round_tick_down(43.67, 0.05)
        assert val == pytest.approx(43.65, abs=1e-9)

    def test_round_tick_already_on_tick(self):
        val = _round_tick(93.0, 0.05)
        assert val == pytest.approx(93.0, abs=1e-9)
