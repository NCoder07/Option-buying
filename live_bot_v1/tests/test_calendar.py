"""
test_calendar.py — Tests for expiry selection logic.

Covers:
  - Expiry selection: today not expiry → nearest
  - Expiry selection: today IS expiry → next
  - DTE calculation
  - is_expiry_today
"""

from __future__ import annotations

from datetime import date

import pytest

from bot.calendar import select_expiry, is_expiry_today, dte


_EXPIRY_LIST = [
    "2025-06-12",
    "2025-06-19",
    "2025-06-26",
    "2025-07-03",
]


class TestSelectExpiry:
    def test_not_expiry_day_returns_nearest(self):
        result = select_expiry(_EXPIRY_LIST, today=date(2025, 6, 10))
        assert result == "2025-06-12"

    def test_is_expiry_day_returns_next(self):
        # June 12 is in the list — should return June 19
        result = select_expiry(_EXPIRY_LIST, today=date(2025, 6, 12))
        assert result == "2025-06-19"

    def test_expiry_day_last_in_list_returns_last(self):
        # If today is last expiry and no next available
        result = select_expiry(["2025-07-03"], today=date(2025, 7, 3))
        assert result is None  # no next expiry

    def test_empty_list_returns_none(self):
        result = select_expiry([], today=date(2025, 6, 10))
        assert result is None

    def test_all_past_expiries_returns_none(self):
        result = select_expiry(["2025-01-02", "2025-01-09"], today=date(2025, 6, 10))
        assert result is None

    def test_uses_today_from_list_correctly(self):
        # today is June 19, which IS in list → return June 26
        result = select_expiry(_EXPIRY_LIST, today=date(2025, 6, 19))
        assert result == "2025-06-26"


class TestIsExpiryToday:
    def test_true_when_today_in_list(self):
        assert is_expiry_today(_EXPIRY_LIST, today=date(2025, 6, 12)) is True

    def test_false_when_today_not_in_list(self):
        assert is_expiry_today(_EXPIRY_LIST, today=date(2025, 6, 11)) is False


class TestDTE:
    def test_dte_future(self):
        d = dte("2025-06-19", today=date(2025, 6, 12))
        assert d == 7

    def test_dte_same_day(self):
        d = dte("2025-06-12", today=date(2025, 6, 12))
        assert d == 0

    def test_dte_past(self):
        d = dte("2025-06-05", today=date(2025, 6, 12))
        assert d == -7
