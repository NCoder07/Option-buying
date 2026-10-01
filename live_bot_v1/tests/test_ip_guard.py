"""
test_ip_guard.py — Tests for IP guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from bot.ip_guard import IPGuard


class TestIPGuard:
    def test_passes_when_ips_match(self):
        guard = IPGuard(expected_ip="1.2.3.4")
        with patch("bot.ip_guard.get_public_ip", return_value="1.2.3.4"):
            assert guard.check(mode="live") is True

    def test_fails_live_on_mismatch(self):
        alerts = MagicMock()
        guard = IPGuard(expected_ip="1.2.3.4", alerts=alerts)
        with patch("bot.ip_guard.get_public_ip", return_value="5.6.7.8"):
            result = guard.check(mode="live")
        assert result is False
        alerts.ip_mismatch.assert_called_once()

    def test_allows_paper_on_mismatch_with_flag(self):
        guard = IPGuard(expected_ip="1.2.3.4")
        with patch("bot.ip_guard.get_public_ip", return_value="5.6.7.8"):
            result = guard.check(mode="paper", allow_mismatch_in_paper=True)
        assert result is True

    def test_no_expected_ip_skips_check(self):
        guard = IPGuard(expected_ip="")
        with patch("bot.ip_guard.get_public_ip", return_value="1.2.3.4"):
            assert guard.check(mode="live") is True

    def test_stores_last_checked_ip(self):
        guard = IPGuard(expected_ip="1.2.3.4")
        with patch("bot.ip_guard.get_public_ip", return_value="1.2.3.4"):
            guard.check(mode="paper")
        assert guard.last_checked_ip == "1.2.3.4"
