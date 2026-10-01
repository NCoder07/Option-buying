"""
test_risk_guard.py — Tests for circuit breakers and risk checks.

Covers:
  - Max orders per day
  - Max daily loss halt
  - Consecutive rejections
  - may_enter flag
  - stop_entries / resume
  - Premium sanity check
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from bot.risk_guard import RiskGuard, RiskState


def _make_guard(overrides: dict = None):
    config = {
        "max_orders_per_day": 10,
        "max_daily_loss_inr": 20000,
        "max_consecutive_rejections": 3,
        "pretrade_check_funds": False,
        "pretrade_check_premium_sanity": True,
        "premium_sanity_min": 1.0,
        "premium_sanity_max": 500.0,
        "max_order_value": 200_000,
        "max_lots": 2,
    }
    if overrides:
        config.update(overrides)
    state = RiskState()
    return RiskGuard(config, state), state


class TestMayEnter:
    def test_initially_may_enter(self):
        guard, _ = _make_guard()
        assert guard.may_enter() is True

    def test_stop_entries_blocks(self):
        guard, _ = _make_guard()
        guard.stop_entries("test")
        assert guard.may_enter() is False

    def test_resume_re_enables(self):
        guard, _ = _make_guard()
        guard.stop_entries("test")
        guard.resume_entries()
        assert guard.may_enter() is True


class TestMaxOrders:
    def test_halts_at_max(self):
        guard, state = _make_guard({"max_orders_per_day": 3})
        for _ in range(3):
            state.orders_today += 1
        assert guard.check_max_orders() is False
        assert guard.may_enter() is False

    def test_allows_below_max(self):
        guard, state = _make_guard({"max_orders_per_day": 10})
        state.orders_today = 5
        assert guard.check_max_orders() is True


class TestMaxDailyLoss:
    def test_halts_on_max_loss(self):
        guard, state = _make_guard({"max_daily_loss_inr": 20000})
        state.daily_loss_inr = -20001
        assert guard.check_daily_loss() is False
        assert guard.may_enter() is False

    def test_allows_within_limit(self):
        guard, state = _make_guard({"max_daily_loss_inr": 20000})
        state.daily_loss_inr = -15000
        assert guard.check_daily_loss() is True


class TestConsecutiveRejections:
    def test_circuit_breaker_at_threshold(self):
        guard, _ = _make_guard({"max_consecutive_rejections": 3})
        guard.record_rejection()
        guard.record_rejection()
        triggered = guard.record_rejection()
        assert triggered is True
        assert guard.may_enter() is False

    def test_rejection_counter_resets_on_fill(self):
        guard, state = _make_guard()
        guard.record_rejection()
        guard.record_rejection()
        guard.record_fill()
        assert state.consecutive_rejections == 0


class TestPremiumSanity:
    def test_rejects_below_min(self):
        guard, _ = _make_guard()
        assert guard.check_premium_sanity(0.5) is False

    def test_rejects_above_max(self):
        guard, _ = _make_guard()
        assert guard.check_premium_sanity(600.0) is False

    def test_accepts_in_range(self):
        guard, _ = _make_guard()
        assert guard.check_premium_sanity(65.0) is True


class TestMaxLots:
    def test_rejects_over_limit(self):
        guard, _ = _make_guard({"max_lots": 2})
        assert guard.check_max_lots(3) is False

    def test_accepts_at_limit(self):
        guard, _ = _make_guard({"max_lots": 2})
        assert guard.check_max_lots(2) is True
