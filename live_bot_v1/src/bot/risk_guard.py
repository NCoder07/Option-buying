"""
risk_guard.py — Pre-trade and runtime circuit breakers.

All checks are stateless and pure functions so they are easy to unit-test.
The lifecycle loop calls these before every order and at key decision points.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Mutable state tracked by the risk guard across a trading session."""
    orders_today: int = 0
    consecutive_rejections: int = 0
    daily_loss_inr: float = 0.0
    entries_halted: bool = False
    halt_reason: Optional[str] = None
    # Flags that can be toggled via botctl
    stop_entries_flag: bool = False


class RiskGuard:
    """
    Stateful circuit-breaker checks.

    Parameters
    ----------
    config   : YAML config dict
    state    : RiskState (shared across the session)
    alerts   : Alerts instance
    data_dir : directory to monitor for disk space
    """

    def __init__(
        self,
        config: dict,
        state: RiskState,
        alerts=None,
        data_dir: Optional[Path] = None,
    ) -> None:
        self._cfg = config
        self._state = state
        self._alerts = alerts
        self._data_dir = data_dir

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def may_enter(self) -> bool:
        """Return True if new entries are permitted."""
        if self._state.stop_entries_flag:
            logger.info("RiskGuard: entries halted (stop_entries_flag)")
            return False
        if self._state.entries_halted:
            logger.info("RiskGuard: entries halted (%s)", self._state.halt_reason)
            return False
        return True

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def check_max_orders(self) -> bool:
        max_orders = int(self._cfg.get("max_orders_per_day", 10))
        if self._state.orders_today >= max_orders:
            self._halt(f"max_orders_per_day={max_orders} reached")
            return False
        return True

    def check_daily_loss(self) -> bool:
        limit = float(self._cfg.get("max_daily_loss_inr", 20000))
        if self._state.daily_loss_inr <= -abs(limit):
            self._halt(f"max_daily_loss ₹{limit:,.0f} exceeded (daily_loss=₹{self._state.daily_loss_inr:,.0f})")
            return False
        return True

    def check_funds(self, broker, symbol: str, qty: int, ltp: float) -> bool:
        if not self._cfg.get("pretrade_check_funds", True):
            return True
        try:
            balance = broker.get_available_balance()
            required = ltp * qty * 1.1  # 10% headroom
            max_order_value = float(self._cfg.get("max_order_value", 200_000))
            if required > max_order_value:
                logger.warning(
                    "check_funds: required=₹%.0f > max_order_value=₹%.0f",
                    required, max_order_value
                )
                return False
            if balance < required:
                logger.warning(
                    "check_funds: balance=₹%.0f < required=₹%.0f",
                    balance, required
                )
                if self._alerts:
                    self._alerts.send(
                        f"Insufficient funds: balance=₹{balance:.0f} required=₹{required:.0f}",
                        level="WARN"
                    )
                return False
        except Exception as e:
            logger.warning("check_funds failed: %s — allowing", e)
        return True

    def check_premium_sanity(self, ltp: float) -> bool:
        if not self._cfg.get("pretrade_check_premium_sanity", True):
            return True
        lo = float(self._cfg.get("premium_sanity_min", 1.0))
        hi = float(self._cfg.get("premium_sanity_max", 500.0))
        if not (lo <= ltp <= hi):
            logger.warning("check_premium_sanity: LTP=%.2f outside [%.2f, %.2f]", ltp, lo, hi)
            return False
        return True

    def check_max_lots(self, lots: int) -> bool:
        max_lots = int(self._cfg.get("max_lots", 2))
        if lots > max_lots:
            logger.warning("check_max_lots: lots=%d > max_lots=%d", lots, max_lots)
            return False
        return True

    def check_disk_space(self) -> bool:
        if self._data_dir is None:
            return True
        try:
            usage = shutil.disk_usage(str(self._data_dir.parent))
            free_gb = usage.free / 1e9
            threshold = float(self._cfg.get("min_disk_free_gb", 1.0))
            if free_gb < threshold:
                self._halt(f"Disk free={free_gb:.2f}GB < threshold={threshold}GB")
                if self._alerts:
                    self._alerts.disk_low(free_gb)
                return False
        except Exception as e:
            logger.warning("check_disk_space failed: %s", e)
        return True

    # ------------------------------------------------------------------
    # Post-order feedback
    # ------------------------------------------------------------------

    def record_order_sent(self) -> None:
        self._state.orders_today += 1

    def record_rejection(self) -> bool:
        """Call after a REJECTED order.  Returns True if circuit breaker triggered."""
        self._state.consecutive_rejections += 1
        max_rej = int(self._cfg.get("max_consecutive_rejections", 3))
        if self._state.consecutive_rejections >= max_rej:
            self._halt(
                f"max_consecutive_rejections={max_rej} reached"
            )
            if self._alerts:
                self._alerts.circuit_breaker(
                    f"{max_rej} consecutive rejections"
                )
            return True
        return False

    def record_fill(self) -> None:
        self._state.consecutive_rejections = 0

    def record_pnl(self, pnl_inr: float) -> None:
        self._state.daily_loss_inr += pnl_inr

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _halt(self, reason: str) -> None:
        if not self._state.entries_halted:
            logger.error("RiskGuard: halting entries — %s", reason)
            self._state.entries_halted = True
            self._state.halt_reason = reason
            if self._alerts:
                self._alerts.circuit_breaker(reason)

    def stop_entries(self, reason: str = "manual") -> None:
        self._state.stop_entries_flag = True
        logger.warning("RiskGuard: stop_entries flag set (%s)", reason)

    def resume_entries(self) -> None:
        self._state.stop_entries_flag = False
        self._state.entries_halted = False
        self._state.halt_reason = None
        logger.info("RiskGuard: entries resumed")

    def status_dict(self) -> dict:
        return {
            "entries_halted": self._state.entries_halted,
            "halt_reason": self._state.halt_reason,
            "stop_entries_flag": self._state.stop_entries_flag,
            "orders_today": self._state.orders_today,
            "consecutive_rejections": self._state.consecutive_rejections,
            "daily_loss_inr": self._state.daily_loss_inr,
        }
