"""
clock_guard.py — Verify VM clock drift is within tolerance.

Method priority:
  1. ntplib  — direct NTP query, pure UTC epoch comparison, works on all OSes
  2. chronyc — Linux only (skipped silently on Windows)
  3. timedatectl — Linux only
  4. HTTPS Date header — last resort, timezone-safe version

All comparisons are done in UTC epoch seconds (time.time() output) so the
VM system timezone never affects the result.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def _drift_from_ntplib() -> Optional[float]:
    """
    Query an NTP server directly using ntplib.
    Returns absolute offset in seconds.

    This is the most reliable method on all platforms including Windows.
    ntplib compares local UTC epoch time against the NTP server's UTC epoch
    time — timezone is irrelevant.
    """
    try:
        import ntplib
        c = ntplib.NTPClient()
        # pool.ntp.org is load-balanced and reliable worldwide
        response = c.request("pool.ntp.org", version=3, timeout=5)
        return abs(response.offset)
    except Exception:
        pass
    return None


def _drift_from_chronyc() -> Optional[float]:
    """Return system clock offset in seconds from chronyc tracking (Linux only)."""
    try:
        out = subprocess.check_output(
            ["chronyc", "tracking"], stderr=subprocess.STDOUT, timeout=5
        ).decode()
        for line in out.splitlines():
            if "System time" in line and "seconds" in line:
                parts = line.split(":")
                if len(parts) > 1:
                    token = parts[1].strip().split()[0]
                    return abs(float(token))
    except Exception:
        pass
    return None


def _drift_from_timedatectl() -> Optional[float]:
    """Return NTP sync status from timedatectl (Linux only)."""
    try:
        out = subprocess.check_output(
            ["timedatectl", "status"], stderr=subprocess.STDOUT, timeout=5
        ).decode()
        for line in out.splitlines():
            if "synchronized" in line.lower() and "yes" in line.lower():
                return 0.0  # synced — report 0 drift
    except Exception:
        pass
    return None


def _drift_from_https() -> Optional[float]:
    """
    Compare local UTC epoch time against HTTPS Date header.

    Uses calendar.timegm to parse the RFC 2822 Date header into a UTC epoch
    timestamp — this is timezone-safe and avoids the Windows bug where
    parsedate_to_datetime().timestamp() returns local-time seconds.
    """
    try:
        import calendar
        from email.utils import parsedate
        t0 = time.time()
        resp = requests.head("https://www.google.com", timeout=10)
        t1 = time.time()
        date_hdr = resp.headers.get("Date")
        if date_hdr:
            parsed = parsedate(date_hdr)       # returns a time.struct_time (UTC)
            if parsed:
                server_utc = calendar.timegm(parsed)   # always UTC, no TZ conversion
                local_mid = (t0 + t1) / 2
                return abs(local_mid - server_utc)
    except Exception:
        pass
    return None


def measure_drift() -> Optional[float]:
    """
    Try to measure clock drift in seconds.
    Returns None if all methods fail (treat as unknown, not as zero).

    ntplib is tried first — it is the most accurate and works on Windows.
    """
    for fn in [_drift_from_ntplib, _drift_from_chronyc,
               _drift_from_timedatectl, _drift_from_https]:
        drift = fn()
        if drift is not None:
            return drift
    return None


class ClockGuard:
    """
    Checks clock drift at startup and periodically.

    Parameters
    ----------
    max_drift_sec   : tolerance in seconds (default 2.0)
    alerts          : Alerts instance
    """

    def __init__(self, max_drift_sec: float = 2.0, alerts=None) -> None:
        self._max_drift = max_drift_sec
        self._alerts = alerts
        self._last_drift: Optional[float] = None

    def check(self) -> bool:
        """Return True if clock drift is within tolerance."""
        drift = measure_drift()
        self._last_drift = drift

        if drift is None:
            logger.warning("ClockGuard: could not measure drift — assuming OK")
            return True

        if drift <= self._max_drift:
            logger.info("ClockGuard: drift=%.3fs (max=%.1fs) OK", drift, self._max_drift)
            return True

        logger.error(
            "ClockGuard: drift=%.3fs exceeds max=%.1fs — halting new entries",
            drift, self._max_drift
        )
        if self._alerts:
            self._alerts.clock_drift(drift)
        return False

    @property
    def last_drift(self) -> Optional[float]:
        return self._last_drift
