"""
ip_guard.py — Verify the VM's outbound public IP matches the expected static IP.

Called at startup and before the first order each day.
On mismatch: refuses live trading and sends an alert.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def get_public_ip(timeout: int = 10) -> Optional[str]:
    """Return the VM's outbound public IP as seen by the internet."""
    for url in [
        "https://api.ipify.org?format=json",
        "https://api4.my-ip.io/ip.json",
    ]:
        try:
            resp = requests.get(url, timeout=timeout)
            data = resp.json()
            ip = data.get("ip") or data.get("IP") or str(data)
            return ip.strip()
        except Exception:
            continue
    logger.error("Could not determine public IP from any endpoint")
    return None


class IPGuard:
    """
    Checks that the VM's outbound IP matches the configured expected_static_ip.

    Parameters
    ----------
    expected_ip : the static IP whitelisted in Dhan (from env EXPECTED_STATIC_IP)
    alerts : Alerts instance for sending mismatch notifications
    """

    def __init__(self, expected_ip: str, alerts=None) -> None:
        self._expected = (expected_ip or "").strip()
        self._alerts = alerts
        self._last_checked_ip: Optional[str] = None

    def check(self, *, allow_mismatch_in_paper: bool = True, mode: str = "paper") -> bool:
        """
        Returns True if IP check passes (or is skipped).

        In paper mode with allow_mismatch_in_paper=True, a mismatch is a warning
        but does not block.  In live mode, a mismatch always blocks.
        """
        if not self._expected:
            logger.warning("IPGuard: EXPECTED_STATIC_IP not configured — skipping check")
            return True

        actual = get_public_ip()
        self._last_checked_ip = actual

        if actual is None:
            msg = "IPGuard: could not determine public IP — proceeding cautiously"
            logger.warning(msg)
            if mode == "live":
                if self._alerts:
                    self._alerts.send(msg, level="ERROR")
                return False
            return True

        if actual == self._expected:
            logger.info("IPGuard: IP check passed (%s)", actual)
            return True

        msg = (
            f"IPGuard: IP MISMATCH — outbound={actual}, expected={self._expected}. "
            f"mode={mode}"
        )
        logger.error(msg)
        if self._alerts:
            self._alerts.ip_mismatch(actual, self._expected)

        if mode == "live":
            return False

        # Paper mode: warn but allow
        if allow_mismatch_in_paper:
            logger.warning("IPGuard: allowing paper mode despite IP mismatch")
            return True
        return False

    @property
    def last_checked_ip(self) -> Optional[str]:
        return self._last_checked_ip
